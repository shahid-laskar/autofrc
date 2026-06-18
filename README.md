# FRC Pyro Recharge Service — Complete Guide
---

## Table of Contents

1. [What does this service do?](#1-what-does-this-service-do)
2. [How it fits into the bigger picture](#2-how-it-fits-into-the-bigger-picture)
3. [The complete data flow — step by step](#3-the-complete-data-flow--step-by-step)
4. [Project structure explained](#4-project-structure-explained)
5. [Every file explained](#5-every-file-explained)
6. [Database tables explained](#6-database-tables-explained)
7. [Status codes and what they mean](#7-status-codes-and-what-they-mean)
8. [Setting up from scratch](#8-setting-up-from-scratch)
9. [Docker and Docker Compose — what they are and how to use them](#9-docker-and-docker-compose--what-they-are-and-how-to-use-them)
10. [Step-by-step deployment using Docker Compose](#10-step-by-step-deployment-using-docker-compose)
11. [Verifying everything works](#11-verifying-everything-works)
12. [Day-to-day operations](#12-day-to-day-operations)
13. [Troubleshooting common problems](#13-troubleshooting-common-problems)
14. [Quick reference](#14-quick-reference)

---

## 1. What does this service do?

When a new mobile subscriber gets a SIM activated through Sanchar Mitra, they are
entitled to a **First Recharge (FRC)** — a mandatory initial top-up that activates
their prepaid account on the BSNL network.

This service automates that process end to end:

1. Every morning it reads the Oracle database to find newly activated subscribers
   who need an FRC but haven't received one yet.
2. It looks up the correct recharge plan and vendor (ctopup) details from PostgreSQL.
3. It submits a recharge request to the **Pyro payment gateway API** on behalf of
   the vendor.
4. It waits for Pyro to confirm the recharge was successful (via a callback or by
   polling).
5. It updates all relevant records to reflect the outcome.

Without this service, someone would have to manually trigger FRC recharges for every
new activation — potentially hundreds per day.

---

## 2. How it fits into the bigger picture

```
┌─────────────────────────────────────────────────────────────────┐
│                        BSNL Systems                             │
│                                                                 │
│  Sanchar Mitra App                                              │
│  (dealer/retailer app)                                          │
│       │                                                         │
│       │ Activates subscriber, sets HLR_FINAL_ACT_DATE in BCD   │
│       ▼                                                         │
│  Oracle DB ──────────── BCD Table                               │
│  (CAF_ADMIN schema)     (FRC_FLOW_STATUS starts as 'NP')        │
│                                                                 │
│  PostgreSQL DB ───────── cos_bcd        (EKYC subscriber data)  │
│  (public schema)  ────── ctop_master    (vendor/ctopup details) │
│                   ────── frc_plan_table (recharge plan amounts) │
│                   ────── frc_pyro_request_data  ◄── WE WRITE    │
│                   ────── frc_txn_log            ◄── WE WRITE    │
│                                                                 │
│  THIS SERVICE ◄──────────────────────────────────────────────── │
│  (FRC Pyro Recharge)                                            │
│       │                                                         │
│       │ Sends recharge requests                                 │
│       ▼                                                         │
│  Pyro Payment Gateway API                                       │
│  (external system — Pyro Holdings Pvt. Ltd.)                    │
│       │                                                         │
│       │ Sends back result via callback POST                     │
│       ▼                                                         │
│  THIS SERVICE (receives callback at /smpyro/callback/recharge)  │
└─────────────────────────────────────────────────────────────────┘
```

**Two databases:** This service connects to both Oracle (for BCD) and PostgreSQL
(for everything else). Oracle uses the `cx_Oracle` Python library. PostgreSQL uses
the `psycopg2` library.

**One external API:** Pyro's recharge API. All communication is encrypted using
3DES encryption — the request body is encrypted before sending, and the response
body is encrypted and must be decrypted after receiving.

---

## 3. The complete data flow — step by step

Understanding this flow is the most important thing. Everything else in this document
explains how each step is implemented.

### Phase 1 — Batch Population (runs every 1 hour )

```
STEP 1: Query Oracle BCD table
        Filter: ACTIVATION_STATUS ='C' ← subscriber is activated
                HLR_FINAL_ACT_DATE IS NOT NULL   ← subscriber is activated
                FRC_FLOW_STATUS = 'NP'            ← not yet processed
                FRC_REQID IS NULL                 ← no prior FRC request
        Result: list of GSM numbers + CAF serial numbers

STEP 2: For each GSM number, query Postgres cos_bcd, cos_bcd_dkyc
        Check: frc_plan_name, frc_plan_code, frc_category_code,
               frc_ctopup_number, frc_ctopup_number_mpin are ALL non-null
        If any is null → skip (this subscriber doesn't need FRC)
        in cos_bcd_dkyc, there is no plan_code ??
        Also joins: ctop_master   → get vendorid, vendormsisdn
                    frc_plan_table → get frcamt (the amount to recharge)

STEP 3: Encrypt the MPIN from cos_bcd using 3DES before saving

STEP 4: Insert one row per subscriber into Postgres frc_pyro_request_data
        push_flag = 'N'  (Not yet pushed to Pyro)

STEP 5: Write back to Oracle BCD:
        FRC_FLOW_STATUS = 'RQ'  (Request Queued)
        FRC_REQID = reqid       (links BCD to our new row)
        ← This prevents the same subscriber being picked up tomorrow
```

### Phase 2 — Recharge Dispatch (runs every 30 minutes)

```
STEP 6: Query Postgres frc_pyro_request_data
        Filter: push_flag IN ('N', 'E')  ← new or retry rows

STEP 7: For each row:
        a. Get a fresh actionToken from Pyro API
           (actionToken is single-use, expires in 1 minute)
        b. Build recharge request payload:
           { dealerMsisdn, destMsisdn, amount, clientTxnId, mpin }
        c. Encrypt the entire JSON payload with 3DES
        d. POST to Pyro /epin-vendor-api/recharge
        e. Decrypt the response

STEP 8: Handle response:
        statusCode 2002 → "Registered" — Pyro accepted the request
                          push_flag = 'P'  (Pushed, awaiting callback)
                          BCD FRC_FLOW_STATUS = 'W'  (Waiting)
        statusCode 5006/5007/etc → Data error — cannot retry
                          push_flag = 'F'  (Failed permanently)
                          BCD FRC_FLOW_STATUS = 'ID' (Invalid Data)
        statusCode 500 → Pyro internal error — retry later
                          push_flag = 'E'  (Error, will retry)
```

### Phase 3 — Result Handling (two paths)

**Path A: Callback (preferred — Pyro calls us)**
```
STEP 9a: Pyro POSTs to https://mitra.bsnl.co.in/smpyro/callback/recharge
         Body contains: transactionId, statusCode, dealerBalanceAfter, etc.
         Our service looks up the row using transactionId → pyro_trans_id

STEP 10a: Update Postgres frc_pyro_request_data:
          If statusCode 2000 (SUCCESS): push_flag = 'Y'
          If anything else:             push_flag = 'F'

STEP 11a: Update Oracle BCD:
          If success: FRC_FLOW_STATUS = 'P'  (Processed)
          If failure: FRC_FLOW_STATUS = 'F'  (Failed)
```

**Path B: Status Check / Watchdog (fallback — if no callback after 2 minutes)**
```
STEP 9b: Every 5 minutes, check for rows where:
         push_flag = 'P' AND pushed more than 2 minutes ago

STEP 10b: POST to Pyro /epin-vendor-api/transaction-status
          Response tells us if the recharge succeeded or failed

STEP 11b: Same DB updates as Path A
          Extra: BCD FRC_FLOW_STATUS = 'NR' on first check (No Response received)
```

---

## 4. Project structure explained

```
frc_recharge_service/
│
├── main.py                      ← Entry point. Starts everything.
├── requirements.txt             ← Python packages needed
├── .env                         ← Your secret config (never commit this)
├── .env.example                 ← Template showing what .env should contain
├── Dockerfile                   ← Instructions to build the Docker image
├── docker-compose.yml           ← Runs the app + nginx together
│
├── app/                         ← All application code lives here
│   ├── config.py                ← Reads settings from .env file
│   ├── encryption.py            ← 3DES encrypt/decrypt functions
│   ├── processor.py             ← Recharge dispatch loop
│   ├── status_checker.py        ← Fallback watchdog
│   ├── callback.py              ← Receives results from Pyro
│   ├── scheduler.py             ← Schedules all jobs (cron-like)
│   ├── pyro_client.py           ← Makes HTTP calls to Pyro API
│   │
│   ├── auth/
│   │   └── token_manager.py     ← Manages Pyro API tokens
│   │
│   ├── db/
│   │   ├── oracle.py            ← Oracle BCD table operations
│   │   └── postgres.py          ← PostgreSQL operations
│   │
│   └── batch/
│       └── populator.py         ← Daily batch: Oracle + Postgres → frc_pyro_request_data
│
├── sql/
│   └── 01_create_tables.sql     ← Run this once to create Postgres tables
│
└── nginx/
    ├── conf.d/frc.conf          ← Nginx reverse proxy configuration
    └── ssl/                     ← Put your SSL certificates here
```

**Why this structure?** Each file has a single clear responsibility. If the recharge
logic breaks, you look in `processor.py`. If Oracle queries are slow, you look in
`db/oracle.py`. This makes debugging much easier.

---

## 5. Every file explained

### `main.py` — The entry point

This is where the application starts. When you run `uvicorn main:app`, Python reads
this file first.

It does four things at startup:
1. Creates a connection pool to PostgreSQL (a pool means multiple connections are kept
   ready, so the app doesn't have to reconnect every time it needs to query the DB)
2. Creates a connection pool to Oracle (same concept)
3. Authenticates with the Pyro API to get tokens
4. Starts the scheduler (which will trigger all the timed jobs)

It also defines the FastAPI web application and registers the URL routes (endpoints).

**Key endpoints defined here:**
- `GET /health` — Just returns `{"status": "ok"}`. Used by Docker to check if the
  service is running.
- `POST /admin/trigger-batch-population` — Manually runs the batch job right now
  instead of waiting for 07:00. Useful for testing.
- `POST /admin/trigger-recharge` — Manually runs the recharge dispatch right now.
- `POST /admin/trigger-status-check` — Manually runs the status checker right now.

---

### `app/config.py` — Configuration

This file reads your `.env` file and makes all the settings available throughout
the application.

For example, when any file needs the Pyro API URL, it imports `settings` from this
file and uses `settings.pyro_base_url`. This means you only ever set the URL in one
place (`.env`) and it's available everywhere.

**How pydantic-settings works:** It automatically reads environment variables. The
variable name in `.env` is the field name in uppercase. So `pyro_base_url` in the
Python class maps to `PYRO_BASE_URL` in the `.env` file.

---

### `app/encryption.py` — 3DES Encrypt/Decrypt

Pyro requires all API request bodies to be encrypted before sending, and all
responses are also encrypted. This file handles that.

**The algorithm (you don't need to understand the math, just the concept):**
- The secret key (a string like `7416358166xxxxxx`) is hashed with SHA-1 to
  produce a 20-byte key, then padded to 24 bytes with zeros
- The message is encrypted using Triple-DES in ECB mode
- The result is encoded as Base64 (text-safe representation of binary data)
- Trailing `=` padding is stripped (Pyro's Java implementation does this)

**Two functions:**
- `encrypt(message, secret_key)` — call before sending any request body to Pyro
- `decrypt(encrypted_text, secret_key)` — call after receiving any response from Pyro

**Important:** Pyro callback bodies are NOT encrypted — Pyro decrypts them before
posting to your callback URL. So `callback.py` does NOT call `decrypt()`.

---

### `app/auth/token_manager.py` — Pyro API Authentication

Before making any recharge call, you need to prove who you are to Pyro. This is
done through three types of tokens. This file manages all three.

**sessionToken** (valid 24 hours):
- Obtained by calling `POST /auth-api/authentication` with your loginId and password
- Used as a header in subsequent API calls
- Re-obtained automatically every day at 00:05 by the scheduler

**accessToken** (valid 15 minutes):
- Refreshed using `GET /auth-api/refresh-access-token`
- Must be refreshed before getting an actionToken
- Expiry time is read from the JWT token itself (JWT tokens contain their expiry
  time encoded inside them)

**actionToken** (valid 1 minute, single-use):
- Generated fresh for EVERY recharge call using `GET /auth-api/generate-action-token`
- This is the most important security mechanism — it prevents replaying old requests
- Must be generated immediately before the recharge POST, not earlier

**Why a singleton?** There is only ONE instance of `PyroAuthService` in the whole
application (the `token_manager` object at the bottom of the file). Every part of
the application imports and uses that same object. This ensures tokens are shared
and not duplicated.

---

### `app/db/oracle.py` — Oracle Database (BCD)

This file manages the connection pool to Oracle and all read/write operations on
the `CAF_ADMIN.BCD` table.

**Reading:** `fetch_eligible_bcd_records()` queries BCD for subscribers ready for
FRC. The key filter `FRC_FLOW_STATUS = 'NP'` ensures we never process the same
subscriber twice.

**Writing — 4 functions:**
- `batch_writeback_bcd_rq()` — After batch population succeeds, sets BCD to `'RQ'`
  for all inserted rows. This is the idempotency guard.
- `update_bcd_status()` — Updates a single BCD row's status at each stage.

**Important design choice:** BCD writeback failures are **non-fatal**. The function
wraps every call in `try/except` and logs errors without stopping the main flow. This
is intentional — if the BCD update fails but the Postgres row was inserted, the
Postgres unique constraint will prevent a duplicate insert next time, so the data
stays consistent.

---

### `app/db/postgres.py` — PostgreSQL Database

This is the largest DB file because PostgreSQL holds most of the working data.

**Connection pool:** Uses `psycopg2.pool.ThreadedConnectionPool` which is safe to use
across multiple threads. The context manager `get_pg_conn()` automatically commits
on success and rolls back on error.

**Source data functions (batch population):**
- `fetch_cos_bcd_for_gsms()` — The main join query. For a list of GSM numbers,
  retrieves all FRC-related fields from `cos_bcd`, joined with `ctop_master` (vendor
  details) and `frc_plan_table` (recharge amount). Only returns rows where all five
  FRC indicator fields are non-null.
- `bulk_insert_frc_requests()` — Inserts rows using `execute_batch()` for efficiency.
  Uses `RETURNING reqid, caf_serial_no` so we know which rows were actually inserted
  (some may be skipped by the `ON CONFLICT DO NOTHING` clause).

**State machine functions:**
- `fetch_pending_rows()` — Gets rows with `push_flag IN ('N', 'E')` for processing
- `mark_as_pushed()` — Sets `push_flag = 'P'` after Pyro accepts the request
- `mark_as_success()` — Sets `push_flag = 'Y'` on confirmed success
- `mark_as_failed()` — Sets `push_flag = 'F'` (permanent) or `'E'` (retry)

**Async wrappers:** Because `psycopg2` is a synchronous library (it blocks while
waiting for the database), all functions have `async_*` wrapper versions at the
bottom of the file. These use `asyncio.to_thread()` to run the blocking database
call in a separate thread, freeing up the main event loop to handle other requests
while waiting.

**Transaction log:** `insert_txn_log()` writes one record to `frc_txn_log` for every
API call made. This gives you a complete audit trail. It never raises exceptions —
log failures are just logged.

---

### `app/batch/populator.py` — Daily Batch Population

This file orchestrates Phase 1. It is called every 1 hour and
can also be triggered manually via the admin endpoint.

**Step by step:**
1. Calls `fetch_eligible_bcd_records()` from `oracle.py`
2. Takes the list of GSM numbers and calls `fetch_cos_bcd_for_gsms()` from `postgres.py`
3. Validates each row — checks that vendor details and plan amount were found
4. Encrypts the MPIN using `encrypt()` from `encryption.py`
5. Calls `bulk_insert_frc_requests()` which returns `(reqid, caf_serial_no)` pairs
6. Calls `batch_writeback_bcd_rq()` with those pairs to update Oracle BCD

**Summary dict:** The function returns a dictionary with counts — how many records
were fetched from Oracle, how many had FRC data in Postgres, how many were inserted,
how many were skipped and why. This is logged and returned by the admin endpoint.

**Why the BCD writeback happens last:** If the Postgres insert fails, we don't want
to mark BCD as `RQ` because then the subscriber would never be picked up again.
Postgres goes first, Oracle goes second.

---

### `app/pyro_client.py` — HTTP Client for Pyro API

This file makes the actual HTTP calls to Pyro. It handles:
- Building the request payload
- Encrypting the body
- Making the HTTP POST
- Parsing the response
- Logging every call to `frc_txn_log`

**`recharge()` function:** Called once per row in the processor. Before the POST, it
calls `token_manager.get_action_token()` which always refreshes the access token
first and then generates a fresh action token. This means three API calls happen
before every recharge: refresh_access_token → generate_action_token → recharge.

**`_parse_pyro_response()` helper:** Pyro responses are parsed as plain JSON first,
which matches the current environment. Encrypted response parsing remains as a
fallback for older or different Pyro deployments. Either way, the caller gets a
dict back.

**`_mask_body()` helper:** Before logging the request body to `frc_txn_log`, this
replaces sensitive values (`mpin`, `password`) with `***`. The encrypted body is
never logged — only the plain dict before encryption.

---

### `app/processor.py` — Recharge Dispatch

This file runs the recharge loop — it fetches pending rows and sends them to Pyro.
It runs every 30 minutes.

**For each row:**
- Calls `pyro_client.recharge()` with the subscriber's details
- Based on the Pyro status code, decides what to do next:
  - `2002` → row is registered, waiting for callback. BCD updated to `W`.
  - Permanent error codes → mark as permanently failed. BCD updated to `ID` or `F`.
  - Everything else → mark as retry (`E`). BCD only updated to `F` if max
    retries have been exhausted.

**Retry logic:** `max_retries` defaults to 3. Each time a row is retried, `retry_count`
is incremented. When `retry_count >= max_retries`, the next failure sets BCD to `F`
permanently instead of just `E`.

---

### `app/status_checker.py` — Fallback Watchdog

This runs every 5 minutes and handles the case where Pyro never sends a callback.

**When does this fire?** Only for rows where:
- `push_flag = 'P'` (submitted but no result yet)
- `status_check_eligible_at <= NOW()` (at least 45 seconds since submission — API
  requirement)
- Pushed between 2 minutes and 60 minutes ago (the 2-minute minimum ensures we
  don't check too early; the 60-minute maximum prevents endlessly retrying very old
  submissions)

**On first check:** Sets BCD to `NR` (No Response) to indicate we're actively polling.
On subsequent checks, the BCD status stays `NR` until a definitive result is found.

**901 response:** "No transaction found" — this means Pyro's system hasn't processed
it yet. We set `push_flag = 'E'` so it gets retried on the next status check cycle.

---

### `app/callback.py` — Receives Results from Pyro

This defines the FastAPI endpoint `POST /callback/recharge`. When Pyro finishes
processing a recharge, they POST the result to this URL.

**Critical design rule:** This endpoint always returns HTTP 200, even if something
goes wrong internally. If we return a non-200 response, Pyro will keep retrying the
callback, flooding our server.

**Idempotency guard:** If `push_flag` is already `Y` or `F` (terminal state), the
callback is ignored with a log message. This handles Pyro sending the same callback
twice.

**Lookup:** Pyro sends `transactionId` in the callback body. We use this to find the
matching row in `frc_pyro_request_data` via `pyro_trans_id` column.

---

### `app/scheduler.py` — Job Scheduler

Uses APScheduler (Advanced Python Scheduler) to run four jobs on a cron-like
schedule:

| Job | Schedule | What it does |
|---|---|---|
| `daily_auth` | Every day at 00:05 | Re-authenticates with Pyro to get fresh sessionToken |
| `batch_population` | every 1 hour | Reads Oracle + Postgres, inserts pending rows |
| `recharge_batch` | Every 30 minutes | Processes push_flag N/E rows |
| `status_check` | Every 5 minutes | Checks on push_flag P rows with no callback |



---

## 6. Database tables explained

### Oracle `CAF_ADMIN.BCD`

This is the master subscriber table. We only READ from it (to find eligible
subscribers) and WRITE the FRC flow status back to it. We never create or delete
rows here — Sanchar Mitra manages this table.

**Key columns we care about:**

| Column | What it means |
|---|---|
| `GSMNUMBER` | The subscriber's mobile number |
| `CAF_SERIAL_NO` | Unique ID of the subscriber's CAF form |
| `HLR_FINAL_ACT_DATE` | When the SIM was activated in HLR. Non-null = activated. |
| `FRC_FLOW_STATUS` | Current FRC processing status. Starts as `NP`. |
| `FRC_REQID` | Links to our `reqid` in frc_pyro_request_data |
| `FRC_FLOW_STATUS_UPD_AT` | When we last updated the status |
| `FRC_FLOW_REMARKS` | Human-readable note about what happened |

### Postgres `public.frc_pyro_request_data`

This is our main working table. One row per subscriber per day. The `push_flag`
column drives the state machine.

**Key columns:**

| Column | What it means |
|---|---|
| `reqid` | Auto-generated sequence number. Used as `clientTxnId` sent to Pyro. |
| `gsmno` | Subscriber mobile number |
| `caf_serial_no` | Links back to Oracle BCD |
| `frcamt` | The amount to recharge (from frc_plan_table) |
| `vendormsisdn` | The ctopup/dealer number used for the recharge |
| `mpin` | The dealer's MPIN — stored 3DES encrypted |
| `push_flag` | Current state: N/P/Y/F/E (see table below) |
| `pyro_trans_id` | Pyro's transaction ID — used to match callbacks |
| `msg2pyro` | The plain JSON we sent to Pyro (for debugging) |
| `msg_afterreq` | The decrypted response to our recharge request |
| `msg_aftertr` | The decrypted callback or status check response |
| `batch_date` | The calendar date this row was created |

### Postgres `public.frc_txn_log`

An append-only audit log. One row is inserted for every HTTP call to Pyro and every
callback received. Rows are never updated — only inserted. Used for debugging and
reporting.

---

## 7. Status codes and what they mean

### `push_flag` in `frc_pyro_request_data`

| Value | Name | Meaning |
|---|---|---|
| `N` | Not pushed | Row created by batch, not yet submitted to Pyro |
| `P` | Pushed | Submitted to Pyro, waiting for callback |
| `Y` | Success | Recharge confirmed successful |
| `F` | Failed (permanent) | Error that won't be fixed by retrying |
| `E` | Error (retry) | Transient error — will be retried automatically |

### `frc_flow_status` in Oracle BCD

| Value | Meaning | Set when |
|---|---|---|
| `NP` | Not Processed | Oracle default — we never set this |
| `RQ` | Request Queued | Batch populator inserted the Postgres row |
| `W` | Waiting | Recharge submitted to Pyro, awaiting callback |
| `NR` | No Response | No callback received; status check initiated |
| `P` | Processed | Recharge confirmed successful |
| `ID` | Invalid Data | Permanent data failure (wrong number/plan/MPIN) |
| `F` | Failed | General failure after exhausting retries |

### Pyro API status codes

| Code | Meaning | What we do |
|---|---|---|
| `2002` | Request Registered | Set push_flag=P, BCD=W, wait for callback |
| `2000` | Success | Set push_flag=Y, BCD=P |
| `406` | Account suspended | Permanent failure: push_flag=F, BCD=ID |
| `500` | Pyro internal error | Retry: push_flag=E |
| `506` | Invalid token | Re-authenticate and retry |
| `5006` | Number not found | Permanent failure: push_flag=F, BCD=ID |
| `5007` | Invalid source number | Permanent failure: push_flag=F, BCD=ID |
| `5011` | Wrong denomination | Permanent failure: push_flag=F, BCD=ID |
| `5012` | Invalid MPIN | Permanent failure: push_flag=F, BCD=ID |
| `901` | No transaction found | Not yet processed — retry status check |
| `902` | Transaction failed | Permanent failure: push_flag=F, BCD=F |

---

## 8. Setting up from scratch

### Prerequisites

You need the following installed on the server:

- **Docker** — the container runtime
- **Docker Compose** — the tool to run multiple containers together
- **Your SSL certificate files** — for HTTPS on `mitra.bsnl.co.in`
- Network access to both Oracle DB and PostgreSQL DB from the server
- Network access to the Pyro API server

### Step 1 — Get the code onto the server

Copy the entire `frc_recharge_service/` folder to your server. It should look like:

```
/opt/frc_recharge_service/        ← suggested location
├── main.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env                           ← you will create this
├── .env.example
├── app/
├── sql/
└── nginx/
```

### Step 2 — Create the PostgreSQL tables

Connect to your PostgreSQL database and run the SQL file:

```bash
psql -h YOUR_PG_HOST -U YOUR_PG_USER -d YOUR_PG_DATABASE -f sql/01_create_tables.sql
```

This creates two tables: `frc_pyro_request_data` and `frc_txn_log`. It is safe to
run multiple times — the `IF NOT EXISTS` clause prevents errors.

### Step 3 — Create the `.env` file

```bash
cd /opt/frc_recharge_service
cp .env.example .env
nano .env          # or use any text editor
```

Fill in every value. See the `.env.example` file for the full list with descriptions.
The most important ones:

```env
PYRO_BASE_URL=https://ACTUAL_IP:ACTUAL_PORT
PYRO_API_KEY=the_key_pyro_gave_you
PYRO_LOGIN_ID=the_sanchar_mitra_msisdn
PYRO_PASSWORD=the_password_for_auth_api
PYRO_SECRET_KEY=the_3des_secret_from_pyro

ORACLE_DSN=oracle_host:1521/service_name
ORACLE_USER=CAF_ADMIN
ORACLE_PASSWORD=oracle_password

PG_HOST=postgres_host
PG_DATABASE=your_database_name
PG_USER=your_postgres_user
PG_PASSWORD=your_postgres_password

CALLBACK_BASE_URL=https://mitra.bsnl.co.in/smpyro
```

### Step 4 — Place SSL certificates

```bash
mkdir -p /opt/frc_recharge_service/nginx/ssl

# Copy your certificate files here:
cp /path/to/mitra.bsnl.co.in.crt nginx/ssl/
cp /path/to/mitra.bsnl.co.in.key nginx/ssl/
```

The certificate file (`crt`) should contain the full chain — your domain certificate
plus any intermediate certificates, in that order.

### Step 5 — Tell Pyro your callback URL

Contact Pyro and give them this URL:

```
https://mitra.bsnl.co.in/smpyro/callback/recharge
```

Pyro will POST recharge results to this URL after each transaction completes.

---

## 9. Docker and Docker Compose — what they are and how to use them

### What is Docker?

Think of Docker as a way to package your application along with everything it needs
(Python, all libraries, system tools) into a single self-contained unit called a
**container**. This container runs identically on any server — no "it works on my
machine but not on yours" problems.

A **Docker image** is like a blueprint (read-only template). A **container** is a
running instance of that image. You can run many containers from one image.

A **Dockerfile** is a text file with instructions to build the image — like a recipe.

### What is Docker Compose?

Our application needs two containers running together:
1. The FastAPI application container
2. An Nginx web server container (handles SSL and routes traffic)

Docker Compose lets you define and start both containers with a single command using
a configuration file called `docker-compose.yml`.

### How our Dockerfile works

```dockerfile
# Stage 1: Builder
# Uses a Python 3.11 image to install all Python packages
FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Runtime
# Starts fresh with a minimal image — only copies what's needed
FROM python:3.11-slim

# Create a non-root user for security
# Running as root inside a container is a security risk
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Copy installed packages from the builder stage
COPY --from=builder /install /usr/local

# Copy application code, owned by our non-root user
COPY --chown=appuser:appuser . .

USER appuser

EXPOSE 8000

# Command to start the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000",
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
```

**Why two stages?** The builder stage installs packages (which requires build tools
and creates many temporary files). By starting fresh in stage 2 and only copying
the installed packages, the final image is much smaller and has fewer security
vulnerabilities.

**Why `--workers 1`?** Our application uses APScheduler to run background jobs.
If we ran multiple workers (processes), each worker would start its own scheduler
and the batch job would run multiple times simultaneously, causing duplicate rows.

**Why `--proxy-headers`?** Our app sits behind Nginx. Nginx adds headers like
`X-Forwarded-For` (the real client IP) and `X-Forwarded-Proto` (https). This flag
tells Uvicorn to trust and use those headers.

### How our `docker-compose.yml` works

```yaml
services:
  frc-recharge:          # Our FastAPI application
    build: .             # Build the image using our Dockerfile
    env_file: .env       # Load all environment variables from .env
    expose:
      - "8000"           # Port 8000 is only accessible within Docker network
                         # NOT exposed to the internet directly
    networks:
      - frc_net          # Both services share this internal network

  nginx:                 # The web server / reverse proxy
    image: nginx:1.25-alpine    # Use official Nginx image (pre-built, no Dockerfile needed)
    ports:
      - "80:80"          # Map host port 80 to container port 80 (HTTP)
      - "443:443"        # Map host port 443 to container port 443 (HTTPS)
    volumes:
      - ./nginx/conf.d:/etc/nginx/conf.d:ro   # Mount our nginx config (read-only)
      - ./nginx/ssl:/etc/nginx/ssl:ro         # Mount SSL certs (read-only)
    depends_on:
      frc-recharge:
        condition: service_healthy  # Nginx only starts when our app is healthy
    networks:
      - frc_net
```

**How traffic flows:**

```
Internet
    │
    │ HTTPS port 443
    ▼
Nginx container
    │ /smpyro/ prefix
    │ proxies to frc-recharge:8000
    ▼
FastAPI container (port 8000)
    │
    ├── Reads/writes PostgreSQL
    └── Reads/writes Oracle
```

Nginx handles the SSL certificate (encrypts traffic between browser and server).
The internal connection from Nginx to FastAPI is plain HTTP — it's all inside the
same server so that's fine.

---

## 10. Step-by-step deployment using Docker Compose

### First time deployment

**Step 1 — Go to the project directory:**
```bash
cd /opt/frc_recharge_service
```

**Step 2 — Verify your `.env` file exists and is filled in:**
```bash
cat .env
# Make sure all values are filled — nothing should say 'your_...'
```

**Step 3 — Verify SSL certificates are in place:**
```bash
ls -la nginx/ssl/
# You should see: mitra.bsnl.co.in.crt and mitra.bsnl.co.in.key
```

**Step 4 — Build the Docker image:**

This downloads the base Python image and installs all our dependencies. It only
needs to run when `requirements.txt` or `Dockerfile` changes.

```bash
docker compose build
```

You will see output showing each step of the Dockerfile being executed. This can
take a few minutes the first time.

**Step 5 — Start the containers:**
```bash
docker compose up -d
```

The `-d` flag means "detached" — containers run in the background. Without it,
the logs would stream to your terminal and stopping the terminal would stop the
containers.

**Step 6 — Check that containers are running:**
```bash
docker compose ps
```

Expected output:
```
NAME                  STATUS                   PORTS
frc_recharge_service  Up 30 seconds (healthy)
frc_nginx             Up 5 seconds             0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
```

The `(healthy)` status means Docker's health check (`GET /health`) is passing.

**Step 7 — Check the logs:**
```bash
docker compose logs frc-recharge
```

You should see startup messages like:
```
Starting up...
Oracle pool initialised
Postgres pool initialised
Pyro authentication successful — user: BSNL1
Scheduler started — batch_pop: 07:00 | recharge: */30min | ...
FRC Pyro Recharge Service — ready
```

If authentication fails, you'll see an error here. Check your `PYRO_*` values in `.env`.

**Step 8 — Test the health endpoint:**
```bash
curl https://mitra.bsnl.co.in/smpyro/health
# Expected: {"status":"ok"}
```

**Step 9 — Manually trigger the batch population to test:**
```bash
curl -X POST https://mitra.bsnl.co.in/smpyro/admin/trigger-batch-population
```

Expected response:
```json
{
  "triggered": true,
  "summary": {
    "batch_date": "2025-05-04",
    "oracle_fetched": 45,
    "pg_frc_eligible": 38,
    "inserted": 38,
    "bcd_rq_updated": 38,
    ...
  }
}
```

**Step 10 — Manually trigger recharge dispatch:**
```bash
curl -X POST https://mitra.bsnl.co.in/smpyro/admin/trigger-recharge
```

---

### Updating the application (after code changes)

When you change any Python file:

```bash
cd /opt/frc_recharge_service

# 1. Rebuild the image with updated code
docker compose build frc-recharge

# 2. Restart the service with the new image (zero downtime for nginx)
docker compose up -d frc-recharge
```

Docker Compose will stop the old container and start a new one with the updated image.
Nginx stays running during this process.

### Restarting without rebuilding

If you only changed `.env` settings (not code):

```bash
docker compose up -d frc-recharge
# Docker Compose detects no image change and just restarts with new env
```

### Stopping the service

```bash
docker compose down
```

This stops and removes the containers. The Docker image stays so next `up` is fast.

### Stopping without removing containers

```bash
docker compose stop
# Later: docker compose start
```

---

## 11. Verifying everything works

### Check service health
```bash
curl https://mitra.bsnl.co.in/smpyro/health
# {"status":"ok"}
```

### Check Pyro token status
```bash
curl https://mitra.bsnl.co.in/smpyro/token-status
# {"session_token_present":true,"access_token_present":true,"access_expires_in_s":847}
```

If `session_token_present` is `false`, authentication failed. Check PYRO_* settings.

### Check Swagger docs (interactive API explorer)
Open in browser: `https://mitra.bsnl.co.in/smpyro/docs`

This shows all available endpoints and lets you trigger them directly from the browser.

### Verify batch population ran
```sql
-- In PostgreSQL:
SELECT push_flag, COUNT(*) 
FROM public.frc_pyro_request_data 
WHERE batch_date = CURRENT_DATE
GROUP BY push_flag;
```

Expected after a successful batch run: rows with `push_flag = 'N'` (pending recharge).

### Verify BCD was updated in Oracle
```sql
-- In Oracle:
SELECT FRC_FLOW_STATUS, COUNT(*) 
FROM CAF_ADMIN.BCD 
WHERE FRC_FLOW_STATUS != 'NP'
GROUP BY FRC_FLOW_STATUS;
```

You should see `RQ` counts matching the Postgres insert count.

### Monitor live logs
```bash
# Follow logs in real time
docker compose logs -f frc-recharge

# Last 100 lines
docker compose logs --tail=100 frc-recharge

# Both containers
docker compose logs -f
```

### Check transaction log for a specific subscriber
```sql
-- In PostgreSQL:
SELECT api_stage, pyro_status_code, is_success, duration_ms, logged_at
FROM public.frc_txn_log
WHERE gsmno = '9XXXXXXXXX'
ORDER BY logged_at;
```

---

## 12. Day-to-day operations

### Checking yesterday's recharge results

```sql
SELECT 
    push_flag,
    COUNT(*) as count
FROM public.frc_pyro_request_data
WHERE batch_date = CURRENT_DATE - 1
GROUP BY push_flag;
```

| push_flag | Meaning |
|---|---|
| `Y` | Successfully recharged |
| `F` | Failed permanently — check push_remarks |
| `E` | Failed with retry — will be picked up today |
| `P` | Submitted to Pyro — waiting for callback (unusual if still P next day) |

### Finding failed recharges

```sql
SELECT reqid, gsmno, caf_serial_no, frcamt, push_remarks, last_error_msg, batch_date
FROM public.frc_pyro_request_data
WHERE push_flag = 'F'
ORDER BY batch_date DESC, reqid DESC;
```

### Manually retrying a failed row

If a row is stuck at `F` due to a correctable error, reset it to `N`:

```sql
-- In PostgreSQL:
UPDATE public.frc_pyro_request_data
SET push_flag = 'N', retry_count = 0, push_remarks = 'Manual retry'
WHERE reqid = YOUR_REQID;

-- Also reset BCD in Oracle:
UPDATE CAF_ADMIN.BCD
SET FRC_FLOW_STATUS = 'RQ', FRC_FLOW_REMARKS = 'Manual retry'
WHERE CAF_SERIAL_NO = 'YOUR_CAF_SERIAL_NO';
```

Then trigger recharge:
```bash
curl -X POST https://mitra.bsnl.co.in/smpyro/admin/trigger-recharge
```

### Viewing the API call history for a recharge

```sql
SELECT 
    api_stage, 
    pyro_status_code, 
    pyro_status_text,
    is_success,
    duration_ms,
    error_detail,
    logged_at
FROM public.frc_txn_log
WHERE frc_reqid = YOUR_REQID
ORDER BY logged_at;
```

### Checking if the batch population is overdue

The batch runs at 07:00. If it's 09:00 and no rows were inserted today:

```sql
SELECT COUNT(*) FROM public.frc_pyro_request_data WHERE batch_date = CURRENT_DATE;
-- If 0: batch may have failed
```

Check logs:
```bash
docker compose logs frc-recharge | grep "batch_population"
```

Manually trigger if needed:
```bash
curl -X POST https://mitra.bsnl.co.in/smpyro/admin/trigger-batch-population
```

---

## 13. Troubleshooting common problems

### Container won't start

```bash
docker compose logs frc-recharge
```

**"Cannot connect to Oracle"** — Check `ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD` in `.env`.
Make sure the Oracle server is reachable from the Docker container.

**"Cannot connect to Postgres"** — Check `PG_HOST`, `PG_DATABASE`, `PG_USER`, `PG_PASSWORD`.

**"Pyro authentication failed"** — Check `PYRO_BASE_URL`, `PYRO_API_KEY`, `PYRO_LOGIN_ID`,
`PYRO_PASSWORD`, `PYRO_SECRET_KEY`.

### Nginx returns 502 Bad Gateway

This means Nginx is running but cannot reach the FastAPI container.

```bash
# Check FastAPI container is healthy
docker compose ps

# If not healthy, check its logs
docker compose logs frc-recharge
```

### SSL certificate errors

```bash
# Check certs are in place
ls -la nginx/ssl/

# Check nginx is reading them
docker compose logs nginx
```

Common cause: certificate file contains only the domain cert, not the full chain.
The `.crt` file should contain your domain cert followed by intermediate certs.

### Batch inserted 0 rows but Oracle has eligible records

1. Check that `cos_bcd` has matching GSM numbers with all 5 FRC fields non-null:
```sql
SELECT COUNT(*) FROM public.cos_bcd
WHERE frc_plan_name IS NOT NULL
  AND frc_plan_code IS NOT NULL
  AND frc_category_code IS NOT NULL
  AND frc_ctopup_number IS NOT NULL
  AND frc_ctopup_number_mpin IS NOT NULL;
```

2. Check that `ctop_master` has entries matching `frc_ctopup_number`:
```sql
SELECT cb.frc_ctopup_number, cm.ctopupno 
FROM public.cos_bcd cb
LEFT JOIN public.ctop_master cm ON cm.ctopupno = cb.frc_ctopup_number
WHERE cm.ctopupno IS NULL AND cb.frc_ctopup_number IS NOT NULL
LIMIT 10;
```

3. Check `frc_plan_table` has entries matching `frc_plan_code`:
```sql
SELECT cb.frc_plan_code, fp.plan_code
FROM public.cos_bcd cb
LEFT JOIN public.frc_plan_table fp ON fp.plan_code = cb.frc_plan_code
WHERE fp.plan_code IS NULL AND cb.frc_plan_code IS NOT NULL
LIMIT 10;
```

### Recharge submitted but no callback received

Check status checker logs:
```bash
docker compose logs frc-recharge | grep "STATUS_CHECK"
```

Check the transaction log:
```sql
SELECT * FROM public.frc_txn_log
WHERE api_stage = 'STATUS_CHECK'
ORDER BY logged_at DESC LIMIT 20;
```

Verify the callback URL is registered with Pyro and is publicly accessible:
```bash
curl -X POST https://mitra.bsnl.co.in/smpyro/callback/recharge \
  -H "Content-Type: application/json" \
  -d '{"statusCode":2000,"data":{"transactionId":99999}}'
# Should return: {"received":true,"note":"transaction not found"}
# (not found is fine — it proves the endpoint is reachable)
```

---

## 14. Quick reference

### Docker commands
```bash
docker compose up -d            # Start all containers in background
docker compose down             # Stop and remove containers
docker compose restart          # Restart all containers
docker compose ps               # Show container status
docker compose logs -f          # Follow live logs
docker compose logs frc-recharge # Logs for app container only
docker compose build            # Rebuild image after code changes
docker compose exec frc-recharge bash  # Open shell inside container
```

### Admin endpoints
```bash
# Health check
curl https://mitra.bsnl.co.in/smpyro/health

# Token status
curl https://mitra.bsnl.co.in/smpyro/token-status

# Manually run batch population
curl -X POST https://mitra.bsnl.co.in/smpyro/admin/trigger-batch-population

# Manually run recharge dispatch
curl -X POST https://mitra.bsnl.co.in/smpyro/admin/trigger-recharge

# Manually run status check
curl -X POST https://mitra.bsnl.co.in/smpyro/admin/trigger-status-check

# Interactive API docs
open https://mitra.bsnl.co.in/smpyro/docs
```

### Schedule summary
```
00:05  — Re-authenticate with Pyro (fresh sessionToken)
07:00  — Batch population (Oracle BCD → Postgres frc_pyro_request_data)
*/30m  — Recharge dispatch (push push_flag N/E rows to Pyro)
*/5m   — Status check (check push_flag P rows with no callback)
```

### File to edit for each change

| What you want to change | File to edit |
|---|---|
| Database credentials, Pyro URL/key | `.env` |
| Batch schedule time | `.env` → `BATCH_POPULATION_HOUR` |
| Oracle BCD query / filters | `app/db/oracle.py` |
| Postgres queries | `app/db/postgres.py` |
| cos_bcd join logic | `app/db/postgres.py` → `fetch_cos_bcd_for_gsms()` |
| BCD status codes | `app/db/oracle.py` → `BCD_STATUS_*` constants |
| Recharge retry count | `.env` → `RECHARGE_BATCH_SIZE` or DB `max_retries` column |
| Pyro API endpoints | `app/pyro_client.py` |
| Encryption algorithm | `app/encryption.py` |
| Callback handling | `app/callback.py` |
| Nginx SSL config | `nginx/conf.d/frc.conf` |

### Deployment

On Windows dev PC:
bash# Build for linux/amd64 (server architecture)
docker buildx build --platform linux/amd64 -t frc_recharge_service:v2 .
# use :latest/v2/etc
# Build manually with buildx
docker buildx build --platform linux/amd64 -t frc-recharge:v6 .
docker buildx build --platform linux/amd64 -t frc-nginx:v5 ./nginx


# Save and compress
docker save frc_recharge_service:v2 | gzip > frc_recharge.tar.gz
docker save frc-recharge:v6 frc-nginx:v5 | gzip > frc_images_v5.tar.gz
docker save frc-recharge:v6 | gzip > frc_recharge_v6.tar.gz
# Copy to server (use your server's user and IP)
scp frc_recharge.tar.gz m01400120u1@10.201.222.67:~
scp frc_recharge.tar.gz m01400120u1@10.201.222.67:/home/m01400120u1/autofrc/
scp frc_images_v2.tar.gz m01400120u1@10.201.222.67:/home/m01400120u1/autofrc/
inside server: nano docker-compose.yml paste docker-compose-prod.yml

scp .env m01400120u1@10.201.222.67:/home/m01400120u1/autofrc/


On the server:
cd /opt/autofrc

# Load the image

  gzip -d frc_images_v2.tar.gz
docker load -i frc_images_v2.tar

# Make sure your .env and docker-compose.yml are here
ls -la

# Start the service
docker compose up -d

# Verify
docker compose ps
docker compose logs -f frc-recharge

For subsequent deployments, same steps — rebuild on PC, scp, then on server:
bashdocker compose down
docker load < frc_recharge.tar.gz
docker compose up -d

One thing to update in docker-compose.yml on the server — since the image is pre-built and loaded, remove the build: block so compose doesn't try to build it:
yamlservices:
  frc-recharge:
    image: frc_recharge_service:latest   # ← use loaded image directly
    # build: block removed
    ...

    docker images | grep frc_recharge_service
    docker rmi frc_recharge_service:v1

  curl --ssl-no-revoke --cacert  ./nginx/ssl/internalCA.crt -H "X-Admin-API-Key: bf6f3acd83ab427b4e31b7c2b4ee07a52bde6ee594b3cb1665c35ad6e8ad0356" https://smpyrogateway.bsnl.co.in/api/health


# create directories
sudo mkdir -p /etc/smpyro/nginx/conf.d
sudo mkdir -p /etc/smpyro/nginx/ssl

# copy conf files from your repo
sudo cp nginx/conf.d/frc.conf /etc/smpyro/nginx/conf.d/
sudo cp nginx/ssl/* /etc/smpyro/nginx/ssl/

# ssl keys should be root-readable only
sudo chmod 600 /etc/smpyro/nginx/ssl/*.key
sudo chmod 644 /etc/smpyro/nginx/ssl/*.crt


nginx:
  image: nginx:1.27-alpine        
  container_name: frc_nginx
  restart: unless-stopped
  ports:
    - "80:80"
    - "443:443"
  volumes:
    - /etc/smpyro/nginx/conf.d:/etc/nginx/conf.d:ro   
    - /etc/smpyro/nginx/ssl:/etc/nginx/ssl:ro
  networks:
    - pyro_shared_net
  logging:
    driver: "json-file"
    options:
      max-size: "10m"
      max-file: "5"