
-- TABLE 1: frc_pyro_request_data
--   One row per FRC recharge request.
--   Populated daily by the batch populator from Oracle BCD + Postgres KYC tables.
--   Drives the Pyro recharge state machine.
--
-- TABLE 2: frc_txn_log
--   Immutable append-only audit log of every Pyro API call.
--   One row per HTTP request/response.
--
-- Design decisions:
--   - No UUID: reqid BIGSERIAL is the primary key (simpler, matches clientTxnId)
--   - No process_status: push_flag is the single state machine (tested and working)
--   - No distributed locking fields: single-worker service
--   - No midnight catchup tracking: handled by scheduler re-running on next day
--   - msg_afterreq / msg_aftertr as TEXT (Pyro bodies are encrypted strings, not JSON)
-- =============================================================================

-- =============================================================================
-- TABLE 1: frc_pyro_request_data
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.frc_pyro_request_data (

    -- ── Identity ──────────────────────────────────────────────────────────────
    reqid               BIGSERIAL                                   NOT NULL,
    -- clientTxnId sent to Pyro API = str(reqid), min 5 chars enforced in app
    client_txn_id       VARCHAR(15),

    -- ── Source: Oracle BCD ───────────────────────────────────────────────────
    -- Fields read from Oracle CAF_ADMIN.BCD table via cx_Oracle
    caf_serial_no       VARCHAR(30)                                 NOT NULL,
    gsmno               VARCHAR(10)                                 NOT NULL,
    csccode             VARCHAR(50),
    circle_code         SMALLINT,
    kyc_mode            VARCHAR(5)                                  NOT NULL,
    -- kyc_mode: EKYC only currently (DKYC/SKYC tables lack frc_ fields)
    edate               DATE                                        NOT NULL,
    -- edate = BCD.HLR_FINAL_ACT_DATE (activation gate field)

    -- ── Source: Postgres cos_bcd (EKYC) ─────────────────────────────────────
    -- Five FRC indicator fields — all non-null = FRC required
    frc_plan_name       VARCHAR(100),
    frc_plan_code       VARCHAR(50)                                 NOT NULL,
    frc_category_code   VARCHAR(50)                                 NOT NULL,
    frcamt              INTEGER                                     NOT NULL,
    -- frcamt from frc_plan_table.frc_amount (joined on frc_plan_code)

    -- reqdate = cos_bcd.live_photo_time (subscriber live photo capture time)
    reqdate             TIMESTAMPTZ,

    -- ── Source: Postgres ctop_master ─────────────────────────────────────────
    -- Joined on cos_bcd.frc_ctopup_number = ctop_master.ctopupno
    ctopup_number       VARCHAR(10)                                 NOT NULL,
    -- = cos_bcd.frc_ctopup_number = dealerMsisdn in Pyro API
    vendormsisdn        VARCHAR(10),
    -- = ctop_master.ctopupno (same as ctopup_number in most cases)
    vendorid            VARCHAR(50),
    -- = ctop_master.pos_unique_code

    -- ── MPIN ─────────────────────────────────────────────────────────────────
    -- Source: cos_bcd.frc_ctopup_number_mpin — 3DES encrypted before insert
    mpin                VARCHAR(200)                                NOT NULL,
    mpin_length         SMALLINT,

    -- ── Status flags (set by batch populator at row creation) ────────────────
    in_status           VARCHAR(3)      DEFAULT 'C'                 NOT NULL,
    -- C = Created (by batch job). Not changed by recharge service.
    pyro_status         VARCHAR(3)      DEFAULT 'N'                 NOT NULL,
    -- N = Not sent | REG = Registered (2002) | SUC = Success | FAL = Failed

    -- ── Push state machine ───────────────────────────────────────────────────
    -- N = Not pushed (default, ready for pickup)
    -- P = Pushed to Pyro, awaiting callback
    -- Y = Confirmed success (via callback or status check)
    -- F = Permanent failure (manual fix needed: wrong denom, invalid MPIN, etc.)
    -- E = Transient error (auto-retry on next scheduler run)
    push_flag           VARCHAR(1)      DEFAULT 'N'                 NOT NULL,
    push_remarks        VARCHAR(200),
    push_date           TIMESTAMPTZ,

    -- ── Batch tracking ───────────────────────────────────────────────────────
    batch_date          DATE            DEFAULT CURRENT_DATE         NOT NULL,
    -- Calendar date this row was created. One row per caf_serial_no per day.
    created_at          TIMESTAMPTZ     DEFAULT CURRENT_TIMESTAMP    NOT NULL,

    -- ── Recharge API submission ───────────────────────────────────────────────
    pyro_trans_id       BIGINT,
    -- transactionId from Pyro initial response — join key for callback/status
    pyro_initial_statuscode INTEGER,
    -- statusCode from /recharge response (expect 2002)
    submitted_at        TIMESTAMPTZ,
    -- When /recharge was called
    msg2pyro            TEXT,
    -- Plain-text request JSON sent to Pyro (for audit/replay)
    msg_afterreq        TEXT,
    -- Decrypted initial Pyro response

    -- ── Callback (async result from Pyro) ────────────────────────────────────
    callback_received_at TIMESTAMPTZ,
    dealer_bal_before   NUMERIC(12,2),
    dealer_bal_after    NUMERIC(12,2),
    subscriber_circle   VARCHAR(50),
    msg_aftertr         TEXT,
    -- Decrypted callback or status-check response
    replymsg            VARCHAR(200),
    replyrecvd_date     TIMESTAMPTZ,

    -- ── Status check fallback ────────────────────────────────────────────────
    -- API spec: call /transaction-status minimum 45 seconds after submission
    -- Scheduler enforces 2-minute minimum window
    status_check_eligible_at TIMESTAMPTZ,
    -- Auto-set by trigger: submitted_at + 45 seconds
    status_check_count  SMALLINT        DEFAULT 0                   NOT NULL,
    last_status_check_at TIMESTAMPTZ,

    -- ── Final outcome ─────────────────────────────────────────────────────────
    final_status        VARCHAR(10),
    -- SUCCESS or FAILED (set when push_flag reaches Y or F)
    pyro_final_statuscode INTEGER,
    -- statusCode from callback or status check (2000 = SUCCESS, 902 = FAILED)
    completed_at        TIMESTAMPTZ,

    -- ── Retry tracking ────────────────────────────────────────────────────────
    retry_count         SMALLINT        DEFAULT 0                   NOT NULL,
    max_retries         SMALLINT        DEFAULT 3                   NOT NULL,
    last_error_code     VARCHAR(10),
    last_error_msg      VARCHAR(500),

    -- ── Commission (from callback) ────────────────────────────────────────────
    seller_comm         NUMERIC,
    fra_comm            NUMERIC,

    -- ── Audit ─────────────────────────────────────────────────────────────────
    ipdetail            VARCHAR(20),
    remarks             VARCHAR(500),
    updated_ts          TIMESTAMPTZ     DEFAULT CURRENT_TIMESTAMP    NOT NULL,

    -- ── Constraints ───────────────────────────────────────────────────────────
    CONSTRAINT frc_pyro_pk
        PRIMARY KEY (reqid),

    CONSTRAINT frc_pyro_uq_batch_caf
        UNIQUE (batch_date, caf_serial_no),
    -- One FRC request per CAF serial per calendar day (idempotent batch inserts)

    CONSTRAINT frc_pyro_kyc_chk
        CHECK (kyc_mode IN ('EKYC', 'DKYC', 'SKYC')),

    CONSTRAINT frc_pyro_in_status_chk
        CHECK (in_status IN ('C', 'S', 'F')),

    CONSTRAINT frc_pyro_pyro_status_chk
        CHECK (pyro_status IN ('N', 'REG', 'SUC', 'FAL')),

    CONSTRAINT frc_pyro_push_flag_chk
        CHECK (push_flag IN ('N', 'P', 'Y', 'F', 'E')),

    CONSTRAINT frc_pyro_final_chk
        CHECK (final_status IS NULL OR final_status IN ('SUCCESS', 'FAILED')),

    CONSTRAINT frc_pyro_retry_chk
        CHECK (retry_count <= max_retries + 1)
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

-- Primary batch pickup: push_flag IN ('N','E') and batch_date
CREATE INDEX idx_frc_pyro_pickup
    ON public.frc_pyro_request_data (push_flag, batch_date, created_at);

-- Callback and status check lookup by Pyro transaction ID
CREATE INDEX idx_frc_pyro_trans_id
    ON public.frc_pyro_request_data (pyro_trans_id)
    WHERE pyro_trans_id IS NOT NULL;

-- Status check scheduler: find P rows past the 45s window
CREATE INDEX idx_frc_pyro_status_check
    ON public.frc_pyro_request_data (status_check_eligible_at, push_flag)
    WHERE push_flag = 'P';

-- CAF-level duplicate check
CREATE INDEX idx_frc_pyro_caf
    ON public.frc_pyro_request_data (caf_serial_no, batch_date);

-- GSM-level query
CREATE INDEX idx_frc_pyro_gsmno
    ON public.frc_pyro_request_data (gsmno);

-- BCD writeback completeness check (future use)
CREATE INDEX idx_frc_pyro_final
    ON public.frc_pyro_request_data (final_status, completed_at)
    WHERE final_status IS NOT NULL;

-- ── Trigger: auto-update updated_ts + status_check_eligible_at ───────────────

CREATE OR REPLACE FUNCTION public.trg_frc_pyro_auto_fn()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_ts := CURRENT_TIMESTAMP;

    -- Auto-set status_check_eligible_at when submitted_at is first populated
    IF TG_OP = 'UPDATE'
       AND OLD.submitted_at IS NULL
       AND NEW.submitted_at IS NOT NULL
    THEN
        NEW.status_check_eligible_at :=
            NEW.submitted_at + INTERVAL '45 seconds';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_frc_pyro_auto
BEFORE INSERT OR UPDATE ON public.frc_pyro_request_data
FOR EACH ROW EXECUTE FUNCTION public.trg_frc_pyro_auto_fn();


-- =============================================================================
-- TABLE 2: frc_txn_log
-- =============================================================================
-- Immutable append-only log of every API call to Pyro.
-- Never updated after insert. One row per HTTP request/response.
--
-- api_stage values:
--   AUTH          → POST /auth-api/authentication
--   REFRESH_TOKEN → GET  /auth-api/refresh-access-token
--   ACTION_TOKEN  → GET  /auth-api/generate-action-token
--   RECHARGE      → POST /epin-vendor-api/recharge
--   STATUS_CHECK  → POST /epin-vendor-api/transaction-status
--   CALLBACK_RECV → Pyro POSTed to our callback URL (inbound, not outbound)
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.frc_txn_log (

    -- ── Identity ──────────────────────────────────────────────────────────────
    log_seq             BIGSERIAL                                   NOT NULL,

    -- ── Link to control table ─────────────────────────────────────────────────
    frc_reqid           BIGINT                                      NOT NULL,
    -- References frc_pyro_request_data.reqid (no FK constraint — log is append-only)
    caf_serial_no       VARCHAR(30)                                 NOT NULL,
    gsmno               VARCHAR(10)                                 NOT NULL,
    batch_date          DATE                                        NOT NULL,
    client_txn_id       VARCHAR(15),

    -- ── API call details ──────────────────────────────────────────────────────
    api_stage           VARCHAR(20)                                 NOT NULL,
    api_endpoint        VARCHAR(200),
    http_method         VARCHAR(6),
    attempt_no          SMALLINT        DEFAULT 1                   NOT NULL,

    -- ── Request (masked) ──────────────────────────────────────────────────────
    request_headers     VARCHAR(500),
    -- Header names only, token values replaced with ***
    request_body        TEXT,
    -- Plain-text JSON (before encryption) with sensitive fields masked:
    -- mpin → "***", password → "***"

    -- ── Response ──────────────────────────────────────────────────────────────
    response_http_code  SMALLINT,
    response_body       TEXT,
    -- Decrypted Pyro response JSON stored as text
    pyro_status_code    INTEGER,
    -- statusCode extracted from response (fast filtering without parsing text)
    pyro_status_text    VARCHAR(50),
    pyro_txn_id         BIGINT,

    -- ── Timing ────────────────────────────────────────────────────────────────
    call_started_at     TIMESTAMPTZ                                 NOT NULL,
    call_ended_at       TIMESTAMPTZ,
    duration_ms         INTEGER,

    -- ── Outcome ───────────────────────────────────────────────────────────────
    is_success          VARCHAR(1)                                  NOT NULL,
    -- Y = call succeeded for this stage | N = error or unexpected code
    is_perm_failure     VARCHAR(1)      DEFAULT 'N'                 NOT NULL,
    -- Y = this code means no retry (5006, 5007, 5011, 5012, 406, etc.)

    -- ── Error details ─────────────────────────────────────────────────────────
    error_class         VARCHAR(200),
    -- Exception class name, e.g. 'httpx.TimeoutException'
    error_detail        TEXT,
    -- Full exception message / stack trace

    -- ── Audit ─────────────────────────────────────────────────────────────────
    logged_at           TIMESTAMPTZ     DEFAULT CURRENT_TIMESTAMP   NOT NULL,

    -- ── Constraints ───────────────────────────────────────────────────────────
    CONSTRAINT frc_txn_log_pk
        PRIMARY KEY (log_seq),

    CONSTRAINT frc_txn_log_stage_chk
        CHECK (api_stage IN (
            'AUTH', 'REFRESH_TOKEN', 'ACTION_TOKEN',
            'RECHARGE', 'STATUS_CHECK', 'CALLBACK_RECV'
        )),

    CONSTRAINT frc_txn_log_success_chk
        CHECK (is_success IN ('Y', 'N')),

    CONSTRAINT frc_txn_log_perm_fail_chk
        CHECK (is_perm_failure IN ('Y', 'N'))
);

-- ── Indexes ───────────────────────────────────────────────────────────────────

-- Most common: all API calls for a given request
CREATE INDEX idx_txnlog_reqid
    ON public.frc_txn_log (frc_reqid, api_stage, logged_at);

-- Error analysis by date
CREATE INDEX idx_txnlog_errors
    ON public.frc_txn_log (batch_date, is_success, pyro_status_code);

-- Callback/status check lookup by Pyro txn ID
CREATE INDEX idx_txnlog_pyro_txn
    ON public.frc_txn_log (pyro_txn_id)
    WHERE api_stage IN ('CALLBACK_RECV', 'STATUS_CHECK');

-- Slow call monitoring
CREATE INDEX idx_txnlog_duration
    ON public.frc_txn_log (duration_ms, api_stage);

-- GSM-level history
CREATE INDEX idx_txnlog_gsmno
    ON public.frc_txn_log (gsmno, logged_at);

COMMENT ON TABLE public.frc_pyro_request_data IS
    'FRC recharge request control table. One row per activation per day. '
    'Populated by batch job from Oracle BCD + Postgres KYC tables. '
    'Drives the Pyro recharge state machine via push_flag.';

COMMENT ON TABLE public.frc_txn_log IS
    'Immutable audit log of every Pyro API interaction. '
    'Never updated after insert. Referenced by frc_pyro_request_data.reqid.';