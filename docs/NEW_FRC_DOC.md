# Sanchar Mitra — FRC (First Recharge) Service

A backend service that automates **First Recharge** processing for BSNL subscribers via the Pyro API. It discovers newly activated subscribers, submits recharge requests to Pyro, and handles results via callback or a polling watchdog.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Complete Data Flow — Step by Step](#complete-data-flow--step-by-step)
   - [Phase 1 — Batch Population](#phase-1--batch-population-runs-every-scheduler_batch_population_interval_minutes-default-60-min)
   - [Phase 2 — Recharge Dispatch](#phase-2--recharge-dispatch-runs-every-scheduler_recharge_interval_minutes-default-30-min)
   - [Phase 3 — Result Handling (Callback)](#phase-3a--result-handling-via-callback-preferred)
   - [Phase 3 — Result Handling (Watchdog)](#phase-3b--result-handling-via-watchdog-polling-fallback)
3. [Postgres State Machine](#postgres-state-machine)
4. [Oracle State Machine](#oracle-state-machine)
5. [Encryption](#encryption)
6. [Prerequisites](#prerequisites)
7. [Configuration](#configuration)
8. [Running the Service](#running-the-service)
9. [API Endpoints](#api-endpoints)
10. [Scheduler Jobs](#scheduler-jobs)
11. [Database Schema](#database-schema)

---

## Architecture Overview

The service is built on **FastAPI** with an **APScheduler** background scheduler. Three independent scheduled phases handle batch discovery, recharge dispatch, and result polling. Pyro also pushes results back via an HTTP callback endpoint, which is the preferred result path.

```
┌────────────────────────────────────────────────────────────┐
│                       APScheduler                          │
│  batch_population   recharge_dispatch   watchdog_poll      │
│  (every ~60 min)    (every ~30 min)     (every 5 min)      │
└──────┬──────────────────┬─────────────────────┬────────────┘
       │                  │                     │
       ▼                  ▼                     ▼
┌──────────────┐  ┌───────────────┐   ┌─────────────────────┐
│ Oracle BCD   │  │ Postgres      │   │ Pyro API            │
│ (source)     │  │ frc_pyro_     │   │ /recharge           │
│              │  │ request_data  │   │ /transaction-status │
└──────┬───────┘  └───────────────┘   └─────────────────────┘
       │                                        │
       │                              ┌─────────▼──────────┐
       │                              │ POST /callback/    │
       │                              │ recharge  (Pyro    │
       │                              │ calls us back)     │
       └──────────────────────────────┘
```

---

## Complete Data Flow — Step by Step

### Phase 1 — Batch Population (runs every `SCHEDULER_BATCH_POPULATION_INTERVAL_MINUTES`, default 60 min)

```
STEP 1: Query Oracle BCD table for newly activated subscribers
        Filter:
          ACTIVATION_STATUS = 'C'           ← subscriber is activated
          HLR_FINAL_ACT_DATE IS NOT NULL    ← activation date confirmed
          FRC_FLOW_STATUS = 'NP'            ← not yet processed by FRC
          FRC_REQID IS NULL                 ← no prior FRC request exists
        Result: list of (GSMNUMBER, CAF_SERIAL_NO, ...)

STEP 2: For each GSM number — look up plan and franchise details in Postgres
        Tables queried:
          cos_bcd        → frc_plan_name, frc_plan_code, frc_category_code,
                           frc_ctopup_number, frc_ctopup_number_mpin
          cos_bcd_dkyc   → additional subscriber details
          ctop_master    → vendorid, vendormsisdn (franchise dealer info)
          frc_plan_table → frcamt (the recharge amount for this plan)

        Skip the subscriber if ANY of the following are null:
          frc_plan_name, frc_plan_code, frc_category_code,
          frc_ctopup_number, frc_ctopup_number_mpin
        (These subscribers don't need FRC — no action taken, no writeback.)

STEP 3: Encrypt the MPIN
        frc_ctopup_number_mpin is plain text in Postgres cos_bcd.
        Encrypt it with 3DES using PYRO_SECRET_KEY before storing
        so the debit_txn_log and request table never hold plain MPINs.

STEP 4: Insert one row into Postgres frc_pyro_request_data
        Key fields:
          reqid           = generated UUID
          gsmnumber       = subscriber's GSM number
          plan_code       = from frc_plan_table
          frcamt          = recharge amount
          ctopup_number   = franchise dealer MSISDN
          mpin_encrypted  = 3DES-encrypted MPIN
          push_flag       = 'N'   ← not yet sent to Pyro

STEP 5: Write back to Oracle BCD
        UPDATE BCD
        SET    FRC_FLOW_STATUS = 'RQ',   ← Request Queued
               FRC_REQID       = :reqid  ← links BCD row to our Postgres row
        WHERE  GSMNUMBER        = :gsmnumber

        This prevents the same subscriber from being picked up again
        in the next batch population run.
```

---

### Phase 2 — Recharge Dispatch (runs every `SCHEDULER_RECHARGE_INTERVAL_MINUTES`, default 30 min)

```
STEP 6: Query Postgres frc_pyro_request_data for pending or retry rows
        Filter: push_flag IN ('N', 'E')
          'N' → new, never sent
          'E' → previous attempt errored (Pyro 500); eligible for retry

STEP 7: For each row — authenticate and get a fresh action token
        a. Ensure a valid accessToken via token manager
           (JWT exp checked with 60s buffer; refresh or full re-auth if needed)
        b. POST /auth-api/get-action-token
           Action tokens are SINGLE-USE and expire in ~1 minute.
           Must be fetched immediately before each recharge call.

STEP 8: Build and encrypt the recharge request payload
        {
          "dealerMsisdn":  ctopup_number,
          "destMsisdn":    gsmnumber,
          "amount":        frcamt,
          "clientTxnId":   reqid,
          "mpin":          <decrypted plain text>   ← decrypt in memory only
        }
        Encrypt entire JSON with 3DES-ECB using PYRO_SECRET_KEY.
        The encrypted bytes are posted as raw request body (Content-Type: text/plain).

STEP 9: POST to Pyro
        POST {PYRO_BASE_URL}/epin-vendor-api/recharge
        Headers:
          apiKey:       PYRO_API_KEY
          accessToken:  (JWT)
          sessionToken: (from initial authentication)
          actionToken:  (single-use token from Step 7b)
        Timeout: configurable (default 30s)

STEP 10: Decrypt and parse Pyro response
         Pyro encrypts its response body with 3DES.
         Decrypt using PYRO_SECRET_KEY → parse JSON.

STEP 11: Handle response by statusCode

         ┌─────────────────────────────────────────────────────────┐
         │ statusCode 2002 → "Registered" (async, callback later)  │
         │   push_flag = 'P'   (Pushed, awaiting Pyro callback)    │
         │   pyro_trans_id = transactionId from response           │
         │   BCD: FRC_FLOW_STATUS = 'W'  (Waiting for result)     │
         ├─────────────────────────────────────────────────────────┤
         │ statusCode 5006, 5007, or other data errors             │
         │ → Permanent failure; do not retry                       │
         │   push_flag = 'F'   (Failed permanently)               │
         │   BCD: FRC_FLOW_STATUS = 'ID'  (Invalid Data)          │
         ├─────────────────────────────────────────────────────────┤
         │ statusCode 500 → Pyro internal error                    │
         │ → Transient failure; will retry on next dispatch run    │
         │   push_flag = 'E'   (Error, retry)                     │
         └─────────────────────────────────────────────────────────┘
```

---

### Phase 3a — Result Handling via Callback (preferred)

```
STEP 12: Pyro POSTs to our callback endpoint
         URL: https://smpyrogateway.bsnl.co.in/api/callback/recharge
         Method: POST
         Body: plain JSON (NOT encrypted)

         Key fields in callback body:
           transactionId    → used to look up our Postgres row (pyro_trans_id)
           statusCode       → 2000 = success, anything else = failure
           dealerBalanceAfter, dealerBalanceBefore → logged for audit

STEP 13: Look up frc_pyro_request_data by pyro_trans_id
         If not found → 404 logged and returned to Pyro

STEP 14: Update Postgres frc_pyro_request_data
         If statusCode == 2000 (SUCCESS):
           push_flag = 'Y'   (Complete)
         Else:
           push_flag = 'F'   (Failed)

STEP 15: Update Oracle BCD
         If success:
           FRC_FLOW_STATUS = 'P'   (Processed — final success state)
         If failure:
           FRC_FLOW_STATUS = 'F'   (Failed — final failure state)

STEP 16: Write to FRC transaction log (frc_txn_log) for audit
         Captures: callback body, timing, final status.
```

---

### Phase 3b — Result Handling via Watchdog Polling (fallback)

Runs every `SCHEDULER_WATCHDOG_INTERVAL_MINUTES` (default 5 min) as a safety net for rows where Pyro's callback never arrives.

```
STEP 12: Query Postgres frc_pyro_request_data for stale pushed rows
         Filter:
           push_flag = 'P'                       ← sent, waiting for callback
           pushed_at < NOW() - 2 minutes         ← callback window has passed

STEP 13: For each stale row — POST to Pyro status check endpoint
         POST {PYRO_BASE_URL}/epin-vendor-api/transaction-status
         Body: { "transactionId": pyro_trans_id }

         Decrypt response with 3DES, parse JSON.

STEP 14: First watchdog check — always mark BCD as 'NR' (No Response)
         BCD: FRC_FLOW_STATUS = 'NR'
         (Indicates Pyro callback was missed; status check initiated)

STEP 15: Handle status response
         If statusCode == 2000 (SUCCESS):
           push_flag = 'Y'
           BCD: FRC_FLOW_STATUS = 'P'
         If statusCode indicates failure:
           push_flag = 'F'
           BCD: FRC_FLOW_STATUS = 'F'
         If still pending (Pyro has not resolved yet):
           push_flag remains 'P'
           (watchdog will retry on next 5-min tick)

STEP 16: Write to frc_txn_log for audit
         Captures: status-check request/response, timing.
```

---

## Postgres State Machine

Tracks the lifecycle of each FRC recharge request in `frc_pyro_request_data`.

```
         ┌─────────────────────────────────┐
         │         N  (New row created)    │
         └──────────────┬──────────────────┘
                        │ Phase 2: recharge POST succeeds
                        │ (Pyro responds 2002)
                        ▼
         ┌─────────────────────────────────┐       Phase 2: Pyro 500
         │         P  (Pushed to Pyro)     │◄──────────────────────┐
         └─────┬──────────────┬────────────┘                       │
               │              │                                     │
          callback         watchdog poll                           push_flag='E'
         (statusCode        (after 2 min)                           │
           2000)                │                        ┌──────────┴────────────┐
               │                └─────────────────────►  │    E  (Error/Retry)   │
               ▼                                         └───────────────────────┘
    ┌──────────────────┐
    │    Y  (Complete) │
    └──────────────────┘

    ┌──────────────────┐
    │    F  (Failed)   │  ← permanent failure (data error or Pyro hard reject)
    └──────────────────┘
```

| Flag | Meaning | Set By |
|------|---------|--------|
| `N` | New, not yet sent to Pyro | Phase 1 batch population |
| `P` | Sent to Pyro (statusCode 2002), awaiting callback | Phase 2 dispatch |
| `E` | Pyro returned 500; will retry | Phase 2 dispatch |
| `Y` | Recharge confirmed successful | Callback or watchdog |
| `F` | Permanently failed | Dispatch (data error) or callback/watchdog |

---

## Oracle State Machine

Tracks the FRC lifecycle on the source BCD table.

```
  NP (Not Processed)
    │
    │  Phase 1: subscriber meets criteria, row inserted to Postgres
    ▼
  RQ (Request Queued)
    │
    │  Phase 2: recharge dispatched to Pyro (statusCode 2002)
    ▼
   W (Waiting for callback / watchdog)
    │
    │  Phase 3 (callback or watchdog)
    ├──→  P  (Processed — success)
    ├──→  F  (Failed — Pyro hard reject or data error)
    └──→  NR (No Response — watchdog ran first check, still pending)

  ID (Invalid Data — permanent failure set at dispatch time)
```

| Status | Meaning | Set By |
|--------|---------|--------|
| `NP` | Not yet processed by FRC | Source system |
| `RQ` | Request queued in Postgres | Phase 1 |
| `W` | Waiting for Pyro result | Phase 2 (on 2002 response) |
| `NR` | No Response — watchdog first check done | Watchdog Phase 3b |
| `P` | Processed successfully | Callback or watchdog Phase 3 |
| `F` | Failed | Callback, watchdog, or data error |
| `ID` | Invalid data — no retry | Phase 2 dispatch |

---

## Encryption

Pyro uses **3DES-ECB** (DESede) for both request body encryption and response body decryption.

```
Key derivation:  SHA-1(secret_key_string) → 20 bytes + 4 zero bytes = 24-byte key
Mode:            ECB (no IV)
Padding:         PKCS5 (8-byte DES block size)
Output:          Base64 without trailing '='
```

**FRC-specific encryption behaviour:**

| Data | Encrypted? | Notes |
|------|-----------|-------|
| Auth request body | Yes | 3DES with PYRO_SECRET_KEY |
| Action token request | Yes | 3DES with PYRO_SECRET_KEY |
| Recharge request body | Yes | Entire JSON payload encrypted |
| Pyro recharge response | Yes | Must decrypt response with PYRO_SECRET_KEY |
| Callback body from Pyro | **No** | Plain JSON — do NOT decrypt |
| MPIN stored in Postgres | Yes | Encrypted before insert; decrypted in memory at dispatch |
| MPIN in audit log | Masked | Replaced with `***` |

Note: This is the **opposite** of the Debit Service — Pyro **encrypts** its recharge responses but returns plain JSON for debit responses. The two services use separate `PyroAuthService` instances and separate secret keys.

---

## Prerequisites

- **Python 3.10+** (if running locally)
- **Docker & Docker Compose** (if running via containers)
- Access to the **Oracle Database** (`BCD` table with `FRC_FLOW_STATUS` and `FRC_REQID` columns)
- Access to the **PostgreSQL Database** (for `frc_pyro_request_data`, `cos_bcd`, `frc_txn_log`)
- Valid credentials for the **Pyro API**
- A publicly reachable HTTPS URL for the **Pyro callback** endpoint

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env`.

### Core Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PYRO_BASE_URL` | Base URL for the Pyro API | _(required)_ |
| `PYRO_API_KEY` | Pyro API key | _(required)_ |
| `PYRO_LOGIN_ID` | Pyro login ID | _(required)_ |
| `PYRO_PASSWORD` | Pyro password | _(required)_ |
| `PYRO_SECRET_KEY` | 3DES key (shared for FRC recharge) | _(required)_ |
| `PYRO_REQUEST_TIMEOUT_SECONDS` | HTTP timeout | `30.0` |

### Oracle DB

| Variable | Description |
|----------|-------------|
| `ORACLE_USER` | Oracle username |
| `ORACLE_PASSWORD` | Oracle password |
| `ORACLE_DSN` | `host:port/service_name` |

### PostgreSQL DB

| Variable | Description | Default |
|----------|-------------|---------|
| `PG_HOST` | Postgres host | _(required)_ |
| `PG_PORT` | Postgres port | `5432` |
| `PG_DATABASE` | Database name | _(required)_ |
| `PG_USER` | Postgres username | _(required)_ |
| `PG_PASSWORD` | Postgres password | _(required)_ |

### Scheduler

| Variable | Description | Default |
|----------|-------------|---------|
| `ENABLE_SCHEDULER` | Enable background jobs | `true` |
| `SCHEDULER_BATCH_POPULATION_INTERVAL_MINUTES` | Phase 1 interval | `60` |
| `SCHEDULER_RECHARGE_INTERVAL_MINUTES` | Phase 2 interval | `30` |
| `SCHEDULER_WATCHDOG_INTERVAL_MINUTES` | Phase 3b interval | `5` |
| `SCHEDULER_WATCHDOG_GRACE_MINUTES` | Minutes after push before watchdog checks | `2` |

---

## Running the Service

### Using Docker (Recommended for Production)

```bash
# Build and start
docker-compose up -d --build

# View logs
docker logs -f pyro_frc_service
```

### Running Locally (Development)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

uvicorn main:app --host 127.0.0.1 --port 8000
```

---

## API Endpoints

### Health & Readiness

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Basic liveness check. Returns `{"status": "ok"}` |
| `GET` | `/ready` | Checks Oracle pool, Postgres pool, and scheduler status |

### Callback (called by Pyro)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/callback/recharge` | Receives Pyro recharge result callbacks |

### Admin Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/admin/trigger-batch-population` | Manually trigger Phase 1 |
| `POST` | `/admin/trigger-recharge-dispatch` | Manually trigger Phase 2 |
| `POST` | `/admin/trigger-watchdog` | Manually trigger Phase 3b watchdog |

---

## Scheduler Jobs

| Job | Trigger | What it does |
|-----|---------|--------------|
| `batch_population` | Every `SCHEDULER_BATCH_POPULATION_INTERVAL_MINUTES` | Discover newly activated subscribers in Oracle, populate Postgres |
| `recharge_dispatch` | Every `SCHEDULER_RECHARGE_INTERVAL_MINUTES` | Send pending/retry rows to Pyro recharge API |
| `watchdog_poll` | Every `SCHEDULER_WATCHDOG_INTERVAL_MINUTES` | Poll Pyro for status of rows where callback is overdue |
| `daily_auth` | Daily at 00:10 | Re-authenticate Pyro token manager |

---

## Database Schema

### Postgres — `frc_pyro_request_data`

One row per subscriber FRC attempt.

| Column | Type | Description |
|--------|------|-------------|
| `reqid` | UUID | Primary key; sent as `clientTxnId` to Pyro |
| `gsmnumber` | VARCHAR | Subscriber's GSM number |
| `plan_code` | VARCHAR | FRC plan code |
| `frcamt` | NUMERIC | Recharge amount |
| `ctopup_number` | VARCHAR | Franchise dealer MSISDN (`dealerMsisdn`) |
| `mpin_encrypted` | TEXT | 3DES-encrypted MPIN |
| `push_flag` | CHAR(1) | `N` / `P` / `E` / `Y` / `F` — see state machine |
| `pyro_trans_id` | VARCHAR | `transactionId` from Pyro 2002 response |
| `pushed_at` | TIMESTAMPTZ | When Phase 2 sent the request |
| `result_at` | TIMESTAMPTZ | When callback or watchdog resolved the result |
| `created_at` | TIMESTAMPTZ | Row creation time |

### Postgres — `frc_txn_log`

Audit log for all Pyro API calls (recharge + status check).

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Primary key |
| `reqid` | UUID | Foreign key to `frc_pyro_request_data` |
| `api_stage` | VARCHAR | `RECHARGE` / `STATUS_CHECK` / `CALLBACK` |
| `api_endpoint` | VARCHAR | Full URL |
| `request_body` | TEXT | JSON payload with MPIN masked |
| `response_http_code` | SMALLINT | HTTP status |
| `response_body` | TEXT | Decrypted response JSON |
| `pyro_status_code` | INT | `statusCode` from Pyro JSON |
| `pyro_trans_id` | VARCHAR | Pyro transaction ID |
| `call_started_at` | TIMESTAMPTZ | Before HTTP call |
| `call_ended_at` | TIMESTAMPTZ | After HTTP call |
| `duration_ms` | INT | Round-trip milliseconds |
| `is_success` | CHAR(1) | `Y` or `N` |
| `error_class` | VARCHAR | Python exception class if failed |
| `error_detail` | TEXT | Exception message |
| `created_at` | TIMESTAMPTZ | Row creation time |

### Oracle — `BCD` (key FRC columns)

| Column | Written Value | When |
|--------|--------------|------|
| `FRC_FLOW_STATUS` | `RQ` → `W` → `P`/`F`/`NR`/`ID` | Each phase transition |
| `FRC_REQID` | UUID from `frc_pyro_request_data` | Phase 1 writeback |