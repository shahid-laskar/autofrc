-- =============================================================================
-- FILE  : test_frc_schema_v2.sql
-- TABLES: TEST_FRC_DATA  (one row per FRC recharge request — state control)
--         FRC_TXN_LOG    (one row per API call — immutable audit trail)
-- BASED ON:
--   • API spec SCMPREPAID0425 v1.0
--   • Code field mapping provided (ctopData, bcdData, frcPlanData, cosBcdData)
--   • BCD table (CAF_ADMIN.BCD) DDL
--   • Manager requirements: Postgres KYC tables as FRC indicator source,
--     separate transaction log, robust automation support
-- =============================================================================


-- =============================================================================
--  DATA SOURCE REFERENCE
--  This section maps every field to its origin table/column so the
--  batch population query can be written unambiguously.
-- =============================================================================
--
--  SOURCE A — BCD (Oracle CAF_ADMIN.BCD)
--  Filter:   ACTIVATION_STATUS = 'A'  AND  HLR_FINAL_ACT_DATE IS NOT NULL
--  Fields:
--    BCD.GSMNUMBER             → GSMNO
--    BCD.CAF_SERIAL_NO         → CAF_SERIAL_NO
--    BCD.DE_CSCCODE            → CSCCODE
--    BCD.HLR_FINAL_ACT_DATE    → EDATE  (also used as FRC eligibility gate)
--    BCD.CIRCLE_CODE           → CIRCLE_CODE
--    BCD.KYC_MODE              → KYC_MODE  (EKYC / DKYC / SKYC)
--    BCD.FRC_FLOW_STATUS       → must be 'NP' (Not Processed) to include row
--    BCD.FRC_REQID             → must be NULL to avoid duplicate processing
--
--  SOURCE B — Postgres KYC table (table chosen by KYC_MODE)
--    EKYC  → pg_schema.ekyc_subscribers   (or equivalent)
--    DKYC  → pg_schema.dkyc_subscribers
--    SKYC  → pg_schema.skyc_subscribers
--  Join key: gsm_number = BCD.GSMNUMBER
--  FRC eligibility: ALL FIVE fields below must be non-null
--    frc_plan_name             → FRC_PLAN_NAME
--    frc_plan_code             → FRC_PLAN_CODE
--    frc_category_code         → FRC_CATEGORY_CODE
--    frc_ctopup_number         → CTOPUP_NUMBER  (= VENDORMSISDN for API)
--    frc_ctopup_number_mpin    → MPIN source (encrypted before storage)
--
--  SOURCE C — Postgres frc_plan_table
--  Join key: plan_code = frc_plan_code from Source B
--    frc_amount                → FRCAMT  (denomination sent to Pyro API)
--    (plan_name already in Source B)
--
--  SOURCE D — Postgres ctopup_master
--  Join key: ctopup_number = frc_ctopup_number from Source B
--    pos_unique_code           → VENDORID
--    ctopupno                  → SOURCE_MSISDN  (redundant alias for VENDORMSISDN)
--
--  SOURCE E — cosBcdData (COS activation record)
--  Join key: caf_serial_no
--    live_photo_time           → REQDATE  (used as request timestamp baseline)
--
-- =============================================================================


-- =============================================================================
--  TABLE 1: TEST_FRC_DATA
--  Role: State-machine control table.
--        One row per FRC request. Written once by batch job; updated by worker.
-- =============================================================================

CREATE TABLE "CAF_ADMIN"."TEST_FRC_DATA" (

    -- ─── Identity ─────────────────────────────────────────────────────────────
    "ID"                        RAW(16)         DEFAULT SYS_GUID()      NOT NULL,
    "REQID"                     NUMBER(10,0)
                                    GENERATED ALWAYS AS IDENTITY
                                    MINVALUE 1 MAXVALUE 9999999999
                                    INCREMENT BY 1 START WITH 1
                                    CACHE 20 NOORDER NOCYCLE            NOT NULL,
    -- CLIENT_TXN_ID is built from REQID by the worker, e.g. 'FRC' || LPAD(REQID,10,'0')
    -- Must be 5–15 chars per API spec. Stored here once generated.
    "CLIENT_TXN_ID"             VARCHAR2(15),

    -- ─── BCD linkage (Source A) ───────────────────────────────────────────────
    "CAF_SERIAL_NO"             VARCHAR2(30)                            NOT NULL,
    "GSMNO"                     VARCHAR2(10)                            NOT NULL,
    "CSCCODE"                   VARCHAR2(50),
    "CIRCLE_CODE"               NUMBER(2,0),
    "KYC_MODE"                  VARCHAR2(5)                             NOT NULL,
    -- KYC_MODE drives which Postgres table is queried; preserved here for
    -- replay and audit without re-querying Postgres.
    -- Allowed: EKYC, DKYC, SKYC

    -- EDATE = HLR_FINAL_ACT_DATE from BCD.
    -- This is the gate field: row is only created when this is NOT NULL.
    -- Also tells Pyro/IN the activation date context.
    "EDATE"                     DATE                                    NOT NULL,

    -- REQDATE = cosBcdData.live_photo_time (Source E).
    -- Represents the timestamp of the subscriber's live photo capture in COS.
    -- Used as the logical "request raised at" timestamp.
    "REQDATE"                   DATE,

    -- ─── Postgres FRC plan fields (Sources B + C) ─────────────────────────────
    -- These five fields being non-null is THE indicator that FRC is required.
    -- Denormalized into Oracle so the worker never re-queries Postgres.
    "FRC_PLAN_NAME"             VARCHAR2(100),
    "FRC_PLAN_CODE"             VARCHAR2(50)                            NOT NULL,
    "FRC_CATEGORY_CODE"         VARCHAR2(50)                            NOT NULL,
    "FRCAMT"                    NUMBER(6,0)                             NOT NULL,
    -- frcPlanData.frc_amount — denomination sent as `amount` in /recharge API

    -- ─── Vendor / ctopup fields (Source D) ───────────────────────────────────
    -- frc_ctopup_number is used as both the ctopup identity and the
    -- dealerMsisdn / VENDORMSISDN in the Pyro API request.
    "CTOPUP_NUMBER"             VARCHAR2(10)                            NOT NULL,
    -- = frc_ctopup_number from Postgres KYC table = VENDORMSISDN for API

    "VENDORMSISDN"              VARCHAR2(10),
    -- Populated from ctopData.ctopupno (ctopup_master).
    -- In most cases identical to CTOPUP_NUMBER; kept separately to capture
    -- any ctopup_master record that overrides the KYC-table value.

    "VENDORID"                  VARCHAR2(50),
    -- = ctopData.pos_unique_code from ctopup_master

    "SOURCE_MSISDN"             VARCHAR2(10),
    -- = ctopData.ctopupno — duplicate of VENDORMSISDN kept for API payload audit

    -- ─── MPIN ─────────────────────────────────────────────────────────────────
    -- Source: frc_ctopup_number_mpin from Postgres KYC table.
    -- MUST be 3DES-encrypted before inserting. Never store plaintext.
    "MPIN"                      VARCHAR2(200)                           NOT NULL,
    "MPIN_LENGTH"               NUMBER(2,0),
    -- Stored so the worker can validate encrypted length before sending.

    -- ─── Initial status flags (set by batch job at row creation) ─────────────
    -- These mirror the original FRC_REQUEST_DATA conventions.
    "IN_STATUS"                 VARCHAR2(3)     DEFAULT 'C'             NOT NULL,
    -- C = Created. Updated to 'S' (Sent) or 'F' (Failed) by IN system.

    "PYRO_STATUS"               VARCHAR2(3)     DEFAULT 'N'             NOT NULL,
    -- N = Not yet sent to Pyro. Updated to 'S' once submitted, 'C' on callback.
  

    "PUSH_FLAG"                 VARCHAR2(1)     DEFAULT 'N'             NOT NULL,
    -- N = Not yet pushed to processing queue. Set to 'Y' when LOCKED by worker.

    "PUSH_DATE"                 DATE,
    -- Populated at row creation by the batch job (= TRUNC(SYSDATE) or a
    -- calculated push window). Worker may update this when it actually picks up.

    -- ─── State machine (automation layer) ────────────────────────────────────
    "PROCESS_STATUS"            VARCHAR2(20)    DEFAULT 'NEW'           NOT NULL,
    -- NEW          → row created, not yet picked up by any worker
    -- LOCKED       → worker has acquired this row (see LOCK_UNTIL)
    -- AUTH_PENDING → worker is fetching session/access/action tokens
    -- SUBMITTED    → /recharge API called; initial 2002 response received
    -- AWAITING_CB  → waiting for Pyro callback POST
    -- STATUS_CHECK → no callback received; calling /transaction-status
    -- SUCCESS      → terminal: recharge confirmed
    -- FAILED       → terminal: transient failure exhausted retries
    -- RETRY_PENDING→ soft failure; scheduled for next attempt
    -- PERM_FAILED  → terminal: do not retry (bad number, wrong denom, etc.)
    -- MISSED       → batch_date has passed; catchup job re-queued this row

    -- ─── Batch / scheduling ───────────────────────────────────────────────────
    "BATCH_DATE"                DATE            DEFAULT TRUNC(SYSDATE)  NOT NULL,
    -- Calendar date this row belongs to. Workers process BATCH_DATE <= TRUNC(SYSDATE).
    -- Midnight catchup: rows with BATCH_DATE < TRUNC(SYSDATE) and non-terminal
    -- PROCESS_STATUS are flagged MISSED and re-queued.

    "SCHEDULED_AT"              TIMESTAMP WITH TIME ZONE,
    -- When this row first became eligible for pickup. Set at insert.

    "NEXT_RETRY_AT"             TIMESTAMP WITH TIME ZONE,
    -- When the row is eligible for retry. Worker skips rows where this > now.

    "CREATED_AT"                TIMESTAMP WITH TIME ZONE
                                    DEFAULT CURRENT_TIMESTAMP           NOT NULL,

    -- ─── Distributed locking (prevents double-processing) ────────────────────
    -- Worker atomically acquires a row with:
    --   UPDATE TEST_FRC_DATA
    --   SET PROCESS_STATUS='LOCKED', WORKER_ID=:wid,
    --       LOCK_UNTIL=CURRENT_TIMESTAMP + INTERVAL '5' MINUTE,
    --       PUSH_FLAG='Y', PUSH_DATE=SYSDATE
    --   WHERE PROCESS_STATUS IN ('NEW','RETRY_PENDING','MISSED')
    --     AND BATCH_DATE <= TRUNC(SYSDATE)
    --     AND (LOCK_UNTIL IS NULL OR LOCK_UNTIL < CURRENT_TIMESTAMP)
    --     AND (NEXT_RETRY_AT IS NULL OR NEXT_RETRY_AT <= CURRENT_TIMESTAMP)
    --     AND ROWNUM = 1
    --   RETURNING ID, REQID, GSMNO, FRCAMT, CAF_SERIAL_NO, VENDORMSISDN, MPIN
    --   INTO ...
    "WORKER_ID"                 VARCHAR2(200),
    -- e.g. 'celery-worker-1:pid-9821' or hostname:task_id

    "LOCK_UNTIL"                TIMESTAMP WITH TIME ZONE,
    -- Stale-lock watchdog: if PROCESS_STATUS='LOCKED' AND LOCK_UNTIL < now,
    -- the lock timed out — requeue as RETRY_PENDING.

    -- ─── Auth tracking ────────────────────────────────────────────────────────
    "AUTH_ATTEMPT_COUNT"        NUMBER(3,0)     DEFAULT 0               NOT NULL,
    -- Counts how many times tokens were requested for this row.
    -- Shared session/access tokens may be cached at worker level;
    -- action tokens are always per-request (1 minute / single use).

    "AUTH_OBTAINED_AT"          TIMESTAMP WITH TIME ZONE,
    -- When valid access + action tokens were last confirmed for this row.

    -- ─── Recharge API call ────────────────────────────────────────────────────
    "SUBMITTED_AT"              TIMESTAMP WITH TIME ZONE,
    -- When /recharge was called. Trigger auto-sets STATUS_CHECK_ELIGIBLE_AT
    -- = SUBMITTED_AT + 45 seconds.

    "PYRO_INITIAL_STATUSCODE"   NUMBER(5,0),
    -- statusCode from the synchronous /recharge response.
    -- Expect 2002 (Request Registered). Any other value = error.

    "PYRO_TRANS_ID"             NUMBER(15,0),
    -- transactionId from the initial /recharge response.
    -- Used as lookup key for /transaction-status and callback matching.

    -- ─── Callback ─────────────────────────────────────────────────────────────
    "CALLBACK_RECEIVED_AT"      TIMESTAMP WITH TIME ZONE,
    -- When Pyro POSTed to the vendor callback URL.

    "DEALER_BAL_BEFORE"         NUMBER(12,2),
    "DEALER_BAL_AFTER"          NUMBER(12,2),
    -- dealerBalanceBefore / dealerBalanceAfter from callback body.

    "SUBSCRIBER_CIRCLE"         VARCHAR2(50),
    -- `circle` field from callback (e.g. "Telangana"). Cross-check vs CIRCLE_CODE.

    -- ─── Status check fallback ────────────────────────────────────────────────
    -- API spec: wait minimum 45 seconds after submission before calling
    -- /transaction-status. Trigger auto-populates this from SUBMITTED_AT.
    "STATUS_CHECK_ELIGIBLE_AT"  TIMESTAMP WITH TIME ZONE,

    "STATUS_CHECK_COUNT"        NUMBER(3,0)     DEFAULT 0               NOT NULL,
    -- Increment each time /transaction-status is called.

    "LAST_STATUS_CHECK_AT"      TIMESTAMP WITH TIME ZONE,

    -- ─── Final outcome ────────────────────────────────────────────────────────
    "FINAL_STATUS"              VARCHAR2(10),
    -- SUCCESS or FAILED. Set when PROCESS_STATUS reaches a terminal state.

    "PYRO_FINAL_STATUSCODE"     NUMBER(5,0),
    -- statusCode from final authoritative response (callback or status check).
    -- 2000 = SUCCESS, 902 = FAILED, etc. See API response code table.

    "COMPLETED_AT"              TIMESTAMP WITH TIME ZONE,
    -- When terminal state was reached.

    -- ─── Error and retry tracking ─────────────────────────────────────────────
    "RETRY_COUNT"               NUMBER(3,0)     DEFAULT 0               NOT NULL,
    "MAX_RETRIES"               NUMBER(3,0)     DEFAULT 3               NOT NULL,
    -- Set per-row at creation; allows different retry budgets by circle or amount.

    "LAST_ERROR_CODE"           VARCHAR2(10),
    -- Last API statusCode that caused a failure or retry.

    "LAST_ERROR_MSG"            VARCHAR2(500),
    -- Human-readable description of the last error.

    "ERROR_LOG"                 CLOB,
    -- Full structured error history as a JSON array. Appended on each failure.
    -- Format: [{"attempt":1,"ts":"...","stage":"SUBMIT","code":"500","msg":"..."}]
    -- Allows complete replay analysis without parsing FRC_TXN_LOG.

    -- ─── Commission fields (set after SUCCESS from callback) ──────────────────
    "SELLER_COMM"               NUMBER,
    "FRA_COMM"                  NUMBER,

    -- ─── BCD post-success update tracking ─────────────────────────────────────
    -- After SUCCESS, worker must write back to BCD:
    --   BCD.FRC_FLOW_STATUS        = 'P'  (Processed)
    --   BCD.FRC_REQID              = this REQID
    --   BCD.FRC_FLOW_STATUS_UPD_AT = CURRENT_TIMESTAMP
    --   BCD.FRC_FLOW_REMARKS       = (optional summary)
    "BCD_UPDATED"               VARCHAR2(1)     DEFAULT 'N'             NOT NULL,
    "BCD_UPDATED_AT"            TIMESTAMP WITH TIME ZONE,
    -- If SUCCESS but BCD_UPDATED='N', watchdog detects and completes the write.

    -- ─── Midnight catchup tracking ────────────────────────────────────────────
    "IS_MISSED"                 VARCHAR2(1)     DEFAULT 'N'             NOT NULL,
    -- Set to 'Y' by catchup job when BATCH_DATE < TRUNC(SYSDATE) and
    -- PROCESS_STATUS is not terminal.

    "CATCHUP_BATCH_DATE"        DATE,
    -- Date on which the catchup job re-queued this row.

    "CATCHUP_ATTEMPT"           NUMBER(3,0)     DEFAULT 0               NOT NULL,
    -- Prevents infinite catchup loops. Catchup job skips rows where this >= 5.

    -- ─── Misc / audit ─────────────────────────────────────────────────────────
    "IPDETAIL"                  VARCHAR2(20),
    "REMARKS"                   VARCHAR2(500),
    "UPDATED_TS"                TIMESTAMP(6) WITH TIME ZONE
                                    DEFAULT CURRENT_TIMESTAMP           NOT NULL,

    -- ─── Constraints ──────────────────────────────────────────────────────────
    CONSTRAINT "TFRC_PK"
        PRIMARY KEY ("ID"),

    CONSTRAINT "TFRC_UQ_BATCH_CAF"
        UNIQUE ("BATCH_DATE", "CAF_SERIAL_NO"),
        -- One FRC request per CAF per calendar day.

    CONSTRAINT "TFRC_STATUS_CHK"
        CHECK ("PROCESS_STATUS" IN (
            'NEW','LOCKED','AUTH_PENDING','SUBMITTED','AWAITING_CB',
            'STATUS_CHECK','SUCCESS','FAILED','RETRY_PENDING',
            'PERM_FAILED','MISSED'
        )),

    CONSTRAINT "TFRC_FINAL_CHK"
        CHECK ("FINAL_STATUS" IS NULL OR "FINAL_STATUS" IN ('SUCCESS','FAILED')),

    CONSTRAINT "TFRC_KYC_CHK"
        CHECK ("KYC_MODE" IN ('EKYC','DKYC','SKYC')),

    CONSTRAINT "TFRC_IN_STATUS_CHK"
        CHECK ("IN_STATUS" IN ('C','S','F')),
    -- C=Created, S=Sent, F=Failed

    CONSTRAINT "TFRC_PYRO_STATUS_CHK"
        CHECK ("PYRO_STATUS" IN ('N','S','C','F')),
    -- N=Not sent, S=Submitted, C=Callback received, F=Failed
   

    CONSTRAINT "TFRC_PUSH_FLAG_CHK"
        CHECK ("PUSH_FLAG" IN ('N','Y')),

    CONSTRAINT "TFRC_BCD_UPD_CHK"
        CHECK ("BCD_UPDATED" IN ('N','Y')),

    CONSTRAINT "TFRC_MISSED_CHK"
        CHECK ("IS_MISSED" IN ('N','Y')),

    CONSTRAINT "TFRC_RETRY_BUDGET_CHK"
        CHECK ("RETRY_COUNT" <= "MAX_RETRIES" + 1),

    CONSTRAINT "TFRC_UPDTS_NN"
        CHECK ("UPDATED_TS" IS NOT NULL)
);


-- ── Indexes on TEST_FRC_DATA ──────────────────────────────────────────────────

-- Primary worker pickup query
CREATE INDEX "IDX_TFRC_PICKUP"
    ON "CAF_ADMIN"."TEST_FRC_DATA"
    ("PROCESS_STATUS", "BATCH_DATE", "NEXT_RETRY_AT", "LOCK_UNTIL");

-- Midnight catchup job
CREATE INDEX "IDX_TFRC_MISSED"
    ON "CAF_ADMIN"."TEST_FRC_DATA"
    ("BATCH_DATE", "PROCESS_STATUS", "IS_MISSED");

-- Callback receiver looks up by PYRO_TRANS_ID or CLIENT_TXN_ID
CREATE INDEX "IDX_TFRC_CLIENT_TXN"
    ON "CAF_ADMIN"."TEST_FRC_DATA" ("CLIENT_TXN_ID");

CREATE INDEX "IDX_TFRC_PYRO_TRANS"
    ON "CAF_ADMIN"."TEST_FRC_DATA" ("PYRO_TRANS_ID");

-- BCD linkage and subscription-level duplicate check
CREATE INDEX "IDX_TFRC_CAF"
    ON "CAF_ADMIN"."TEST_FRC_DATA" ("CAF_SERIAL_NO", "BATCH_DATE");

CREATE INDEX "IDX_TFRC_GSM"
    ON "CAF_ADMIN"."TEST_FRC_DATA" ("GSMNO", "PROCESS_STATUS");

-- Status check scheduler: find AWAITING_CB rows past the 45s window
CREATE INDEX "IDX_TFRC_CB_ELIGIBLE"
    ON "CAF_ADMIN"."TEST_FRC_DATA"
    ("STATUS_CHECK_ELIGIBLE_AT", "PROCESS_STATUS");

-- Stale lock watchdog
CREATE INDEX "IDX_TFRC_STALE_LOCK"
    ON "CAF_ADMIN"."TEST_FRC_DATA"
    ("LOCK_UNTIL", "PROCESS_STATUS");

-- BCD writeback completeness check
CREATE INDEX "IDX_TFRC_BCD_UPD"
    ON "CAF_ADMIN"."TEST_FRC_DATA"
    ("BCD_UPDATED", "FINAL_STATUS");


-- ── Trigger on TEST_FRC_DATA ──────────────────────────────────────────────────

CREATE OR REPLACE EDITIONABLE TRIGGER CAF_ADMIN.TRG_TFRC_AUTO
BEFORE INSERT OR UPDATE ON CAF_ADMIN.TEST_FRC_DATA
FOR EACH ROW
BEGIN
    :NEW.updated_ts := CURRENT_TIMESTAMP;

    IF INSERTING THEN
        :NEW.created_at   := CURRENT_TIMESTAMP;
        :NEW.scheduled_at := CURRENT_TIMESTAMP;

        IF :NEW.submitted_at IS NOT NULL THEN
            :NEW.status_check_eligible_at :=
                :NEW.submitted_at + INTERVAL '45' SECOND;
        END IF;
    END IF;

    IF UPDATING AND :OLD.submitted_at IS NULL 
                AND :NEW.submitted_at IS NOT NULL THEN
        :NEW.status_check_eligible_at :=
            :NEW.submitted_at + INTERVAL '45' SECOND;
    END IF;
END;


-- =============================================================================
--  TABLE 2: FRC_TXN_LOG
--  Role: Immutable append-only log of every API interaction.
--        Never updated; only inserted. One row per API call made.
--        This is the manager's "transaction log with request/response statuses".
-- =============================================================================
--
--  API_STAGE values and which API endpoint each maps to:
--    AUTH             → POST /auth-api/authentication
--    REFRESH_TOKEN    → POST /auth-api/refresh-access-token
--    ACTION_TOKEN     → POST /auth-api/generate-action-token
--    RECHARGE         → POST /epin-vendor-api/recharge
--    STATUS_CHECK     → POST /epin-vendor-api/transaction-status
--    CALLBACK_RECV    → Not an outbound call; logged when Pyro POSTs to us
-- =============================================================================

CREATE TABLE "CAF_ADMIN"."FRC_TXN_LOG" (

    -- ─── Identity ─────────────────────────────────────────────────────────────
    "ID"                    RAW(16)         DEFAULT SYS_GUID()          NOT NULL,
    "LOG_SEQ"               NUMBER(15,0)
                                GENERATED ALWAYS AS IDENTITY
                                MINVALUE 1 MAXVALUE 999999999999999
                                INCREMENT BY 1 START WITH 1
                                CACHE 50 NOORDER NOCYCLE                NOT NULL,

    -- ─── Link to control table ────────────────────────────────────────────────
    "FRC_REQUEST_ID"        RAW(16)                                     NOT NULL,
    -- FK to TEST_FRC_DATA.ID. Denormalized fields below avoid joins for reporting.

    "REQID"                 NUMBER(10,0)                                NOT NULL,
    -- TEST_FRC_DATA.REQID — human-readable request identifier

    "CAF_SERIAL_NO"         VARCHAR2(30)                                NOT NULL,
    "GSMNO"                 VARCHAR2(10)                                NOT NULL,
    "BATCH_DATE"            DATE                                        NOT NULL,
    "CLIENT_TXN_ID"         VARCHAR2(15),
    -- Populated for RECHARGE and STATUS_CHECK stages; null for AUTH stages.

    -- ─── API interaction details ──────────────────────────────────────────────
    "API_STAGE"             VARCHAR2(20)                                NOT NULL,
    -- AUTH | REFRESH_TOKEN | ACTION_TOKEN | RECHARGE | STATUS_CHECK | CALLBACK_RECV

    "API_ENDPOINT"          VARCHAR2(200),
    -- Full URL called, e.g. https://IP:PORT/epin-vendor-api/recharge
    -- NULL for CALLBACK_RECV (inbound, not outbound).

    "HTTP_METHOD"           VARCHAR2(6),
    -- POST / GET. NULL for CALLBACK_RECV.

    "ATTEMPT_NO"            NUMBER(3,0)     DEFAULT 1                   NOT NULL,
    -- Which retry attempt triggered this call. Matches TEST_FRC_DATA.RETRY_COUNT+1.

    -- ─── Request capture ──────────────────────────────────────────────────────
    "REQUEST_HEADERS_MASKED" VARCHAR2(500),
    -- Header names only, token values replaced with ***
    -- e.g. {"apiKey":"***","sessionToken":"***","accessToken":"***","actionToken":"***"}

    "REQUEST_BODY_MASKED"   CLOB,
    -- Full JSON request body with sensitive fields masked:
    --   mpin → "mpin":"***"
    --   password → "password":"***"
    -- All other fields stored as-is for audit/replay.

    -- ─── Response capture ─────────────────────────────────────────────────────
    "RESPONSE_HTTP_CODE"    NUMBER(3,0),
    -- HTTP status code returned by Pyro server (200, 400, 500, etc.)
    -- For CALLBACK_RECV this is the HTTP code sent back BY US to Pyro.

    "RESPONSE_BODY"         CLOB,
    -- Raw JSON response body from Pyro. For CALLBACK_RECV, this is the
    -- body POSTed by Pyro to our callback URL.

    "PYRO_STATUS_CODE"      NUMBER(5,0),
    -- statusCode extracted from response JSON (2000, 2002, 500, 5006, etc.)
    -- Makes filtering by outcome fast without parsing the CLOB.

    "PYRO_STATUS_TEXT"      VARCHAR2(30),
    -- status field from response JSON: "SUCCESS", "In Process", etc.

    "PYRO_TXN_ID"           NUMBER(15,0),
    -- transactionId from response, if present. Populated for RECHARGE and
    -- STATUS_CHECK stages; null for auth stages.

    -- ─── Timing ───────────────────────────────────────────────────────────────
    "CALL_STARTED_AT"       TIMESTAMP WITH TIME ZONE                    NOT NULL,
    -- When the HTTP request was initiated.

    "CALL_ENDED_AT"         TIMESTAMP WITH TIME ZONE,
    -- When the response was fully received.

    "DURATION_MS"           NUMBER(8,0),
    -- Call duration in milliseconds. Computed: CALL_ENDED_AT - CALL_STARTED_AT.
    -- Useful for SLA monitoring and identifying slow Pyro responses.

    -- ─── Outcome ──────────────────────────────────────────────────────────────
    "IS_SUCCESS"            VARCHAR2(1)                                 NOT NULL,
    -- Y = call succeeded as expected (right statusCode for this stage)
    -- N = call returned an error or unexpected code

    "IS_PERM_FAILURE"       VARCHAR2(1)     DEFAULT 'N'                 NOT NULL,
    -- Y = statusCode indicates no retry should be attempted
    -- (5006, 5007, 5011, 5012, 5001, 5002, 406, 5030)

    -- ─── Error details (populated when IS_SUCCESS = 'N') ─────────────────────
    "ERROR_CLASS"           VARCHAR2(200),
    -- Exception class name if a code-level exception occurred
    -- e.g. 'requests.exceptions.ConnectionError', 'socket.timeout'

    "ERROR_DETAIL"          CLOB,
    -- Full exception message and stack trace, or Pyro error description.

    -- ─── Worker info ──────────────────────────────────────────────────────────
    "WORKER_ID"             VARCHAR2(200),
    -- Same value as TEST_FRC_DATA.WORKER_ID at time of this call.

    "LOGGED_AT"             TIMESTAMP WITH TIME ZONE
                                DEFAULT CURRENT_TIMESTAMP               NOT NULL,

    -- ─── Constraints ──────────────────────────────────────────────────────────
    CONSTRAINT "FTXNLOG_PK"
        PRIMARY KEY ("ID"),

    CONSTRAINT "FTXNLOG_FK_REQUEST"
        FOREIGN KEY ("FRC_REQUEST_ID")
        REFERENCES "CAF_ADMIN"."TEST_FRC_DATA" ("ID"),

    CONSTRAINT "FTXNLOG_STAGE_CHK"
        CHECK ("API_STAGE" IN (
            'AUTH','REFRESH_TOKEN','ACTION_TOKEN',
            'RECHARGE','STATUS_CHECK','CALLBACK_RECV'
        )),

    CONSTRAINT "FTXNLOG_SUCCESS_CHK"
        CHECK ("IS_SUCCESS" IN ('Y','N')),

    CONSTRAINT "FTXNLOG_PERM_FAIL_CHK"
        CHECK ("IS_PERM_FAILURE" IN ('Y','N'))
);


-- ── Indexes on FRC_TXN_LOG ────────────────────────────────────────────────────

-- Most common: look up all calls for a given FRC request
CREATE INDEX "IDX_FTXNLOG_REQUEST"
    ON "CAF_ADMIN"."FRC_TXN_LOG"
    ("FRC_REQUEST_ID", "API_STAGE", "LOGGED_AT");

-- Error analysis: find all failures for a batch date
CREATE INDEX "IDX_FTXNLOG_ERRORS"
    ON "CAF_ADMIN"."FRC_TXN_LOG"
    ("BATCH_DATE", "IS_SUCCESS", "PYRO_STATUS_CODE");

-- Slow-call monitoring
CREATE INDEX "IDX_FTXNLOG_DURATION"
    ON "CAF_ADMIN"."FRC_TXN_LOG"
    ("DURATION_MS", "API_STAGE");

-- Callback-specific lookup by Pyro transaction ID
CREATE INDEX "IDX_FTXNLOG_PYRO_TXN"
    ON "CAF_ADMIN"."FRC_TXN_LOG"
    ("PYRO_TXN_ID")
    WHERE "API_STAGE" IN ('CALLBACK_RECV','STATUS_CHECK');

-- GSM-level history without joining to control table
CREATE INDEX "IDX_FTXNLOG_GSM"
    ON "CAF_ADMIN"."FRC_TXN_LOG"
    ("GSMNO", "LOGGED_AT");


-- =============================================================================
--  PERMANENT FAILURE CODE REFERENCE
--  Worker must set PROCESS_STATUS='PERM_FAILED', IS_PERM_FAILURE='Y' for these
-- =============================================================================
--
--  Pyro code | Condition                        | Action
--  ----------+----------------------------------+-----------------------------
--  5001      | Username incorrect               | PERM_FAILED + alert ops
--  5002      | Wrong password                   | PERM_FAILED + alert ops
--  5006      | Number not in Pyro system        | PERM_FAILED
--  5007      | Invalid source number            | PERM_FAILED + alert ops
--  5011      | Wrong denomination               | PERM_FAILED
--  5012      | Invalid MPIN                     | PERM_FAILED + alert ops
--  5030      | Service class not found          | PERM_FAILED
--  406       | Account suspended / inactive     | PERM_FAILED
--  415       | 15-min block (same no. + amount) | RETRY after 16 minutes
--  405       | Insufficient stock               | RETRY + alert ops (replenish)
--  500       | Pyro internal error              | RETRY with backoff
--  901       | No transaction found             | Only on STATUS_CHECK; retry
--  902       | Transaction failed (status chk)  | FAILED (not perm; log reason)
-- =============================================================================


-- =============================================================================
--  BATCH POPULATION QUERY SKELETON
--  Run by the cron/Celery beat at start of business day (or configured time).
--  Reads BCD + Postgres; inserts eligible rows into TEST_FRC_DATA.
-- =============================================================================
/*
INSERT INTO CAF_ADMIN.TEST_FRC_DATA (
    caf_serial_no, gsmno, csccode, circle_code, kyc_mode, edate, reqdate,
    frc_plan_name, frc_plan_code, frc_category_code, frcamt,
    ctopup_number, vendormsisdn, vendorid, source_msisdn,
    mpin, mpin_length,
    batch_date, scheduled_at,
    process_status, push_flag, push_date,
    in_status, pyro_status, ss_status,
    max_retries
)
SELECT
    b.CAF_SERIAL_NO,
    b.GSMNUMBER,
    b.DE_CSCCODE,
    b.CIRCLE_CODE,
    b.KYC_MODE,
    b.HLR_FINAL_ACT_DATE,          -- EDATE
    c.live_photo_time,              -- REQDATE from cosBcdData
    k.frc_plan_name,
    k.frc_plan_code,
    k.frc_category_code,
    p.frc_amount,                   -- from frc_plan_table join
    k.frc_ctopup_number,
    m.ctopupno,                     -- VENDORMSISDN from ctopup_master
    m.pos_unique_code,              -- VENDORID
    m.ctopupno,                     -- SOURCE_MSISDN
    encrypt_3des(k.frc_ctopup_number_mpin),  -- MPIN encrypted
    LENGTH(k.frc_ctopup_number_mpin),
    TRUNC(SYSDATE),
    CURRENT_TIMESTAMP,
    'NEW', 'N', TRUNC(SYSDATE),
    'C', 'N', 'N',
    3
FROM
    CAF_ADMIN.BCD b
    -- Join to COS BCD record for live_photo_time
    JOIN cos_bcd_data c ON c.caf_serial_no = b.CAF_SERIAL_NO
    -- Dynamic join to correct Postgres KYC table based on kyc_mode
    -- In Oracle DBLink syntax (adjust to your actual link name):
    JOIN kyc_ekyc_subscribers@PGLINK k
        ON k.gsm_number = b.GSMNUMBER
        AND b.KYC_MODE = 'EKYC'
    -- (UNION with DKYC and SKYC joins in full version)
    JOIN frc_plan_table@PGLINK p ON p.plan_code = k.frc_plan_code
    JOIN ctopup_master@PGLINK  m ON m.ctopup_number = k.frc_ctopup_number
WHERE
    b.ACTIVATION_STATUS = 'A'
    AND b.HLR_FINAL_ACT_DATE IS NOT NULL
    AND b.FRC_FLOW_STATUS = 'NP'          -- Not yet processed
    AND b.FRC_REQID IS NULL               -- No prior request
    -- All five FRC indicator fields non-null
    AND k.frc_plan_name          IS NOT NULL
    AND k.frc_plan_code          IS NOT NULL
    AND k.frc_category_code      IS NOT NULL
    AND k.frc_ctopup_number      IS NOT NULL
    AND k.frc_ctopup_number_mpin IS NOT NULL
    -- Exclude today's rows already inserted (idempotency)
    AND NOT EXISTS (
        SELECT 1 FROM CAF_ADMIN.TEST_FRC_DATA t
        WHERE t.CAF_SERIAL_NO = b.CAF_SERIAL_NO
          AND t.BATCH_DATE    = TRUNC(SYSDATE)
    )
;
COMMIT;
*/
