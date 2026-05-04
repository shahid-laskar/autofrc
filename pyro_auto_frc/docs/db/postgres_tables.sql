-- =============================================================================
-- TABLE 1: test_frc_data
-- =============================================================================

CREATE TABLE caf_admin.test_frc_data (

    -- Identity
    id                          UUID            DEFAULT gen_random_uuid()   NOT NULL,
    reqid                       BIGSERIAL                                   NOT NULL,
    client_txn_id               VARCHAR(15),

    -- BCD linkage (sourced from Oracle BCD via ETL/dblink)
    caf_serial_no               VARCHAR(30)                                 NOT NULL,
    gsmno                       VARCHAR(10)                                 NOT NULL,
    csccode                     VARCHAR(50),
    circle_code                 SMALLINT,
    kyc_mode                    VARCHAR(5)                                  NOT NULL,
    edate                       DATE                                        NOT NULL,
    reqdate                     DATE,

    -- Postgres FRC plan fields
    frc_plan_name               VARCHAR(100),
    frc_plan_code               VARCHAR(50)                                 NOT NULL,
    frc_category_code           VARCHAR(50)                                 NOT NULL,
    frcamt                      INTEGER                                     NOT NULL,

    -- Vendor / ctopup fields
    ctopup_number               VARCHAR(10)                                 NOT NULL,
    vendormsisdn                VARCHAR(10),
    vendorid                    VARCHAR(50),
    source_msisdn               VARCHAR(10),

    -- MPIN (3DES encrypted before insert)
    mpin                        VARCHAR(200)                                NOT NULL,
    mpin_length                 SMALLINT,

    -- Initial status flags
    in_status                   VARCHAR(3)      DEFAULT 'C'                 NOT NULL,
    pyro_status                 VARCHAR(3)      DEFAULT 'N'                 NOT NULL,
    push_flag                   VARCHAR(1)      DEFAULT 'N'                 NOT NULL,
    push_date                   DATE,

    -- State machine
    process_status              VARCHAR(20)     DEFAULT 'NEW'               NOT NULL,

    -- Batch / scheduling
    batch_date                  DATE            DEFAULT CURRENT_DATE        NOT NULL,
    scheduled_at                TIMESTAMPTZ,
    next_retry_at               TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ     DEFAULT CURRENT_TIMESTAMP   NOT NULL,

    -- Distributed locking
    worker_id                   VARCHAR(200),
    lock_until                  TIMESTAMPTZ,

    -- Auth tracking
    auth_attempt_count          SMALLINT        DEFAULT 0                   NOT NULL,
    auth_obtained_at            TIMESTAMPTZ,

    -- Recharge API
    submitted_at                TIMESTAMPTZ,
    pyro_initial_statuscode     INTEGER,
    pyro_trans_id               BIGINT,

    -- Callback
    callback_received_at        TIMESTAMPTZ,
    dealer_bal_before           NUMERIC(12,2),
    dealer_bal_after            NUMERIC(12,2),
    subscriber_circle           VARCHAR(50),

    -- Status check
    status_check_eligible_at    TIMESTAMPTZ,
    status_check_count          SMALLINT        DEFAULT 0                   NOT NULL,
    last_status_check_at        TIMESTAMPTZ,

    -- Final outcome
    final_status                VARCHAR(10),
    pyro_final_statuscode       INTEGER,
    completed_at                TIMESTAMPTZ,

    -- Error and retry
    retry_count                 SMALLINT        DEFAULT 0                   NOT NULL,
    max_retries                 SMALLINT        DEFAULT 3                   NOT NULL,
    last_error_code             VARCHAR(10),
    last_error_msg              VARCHAR(500),
    error_log                   JSONB,
    -- Format: [{"attempt":1,"ts":"...","stage":"SUBMIT","code":"500","msg":"..."}]

    -- Commission
    seller_comm                 NUMERIC,
    fra_comm                    NUMERIC,

    -- BCD writeback tracking
    bcd_updated                 VARCHAR(1)      DEFAULT 'N'                 NOT NULL,
    bcd_updated_at              TIMESTAMPTZ,

    -- Midnight catchup
    is_missed                   VARCHAR(1)      DEFAULT 'N'                 NOT NULL,
    catchup_batch_date          DATE,
    catchup_attempt             SMALLINT        DEFAULT 0                   NOT NULL,

    -- Audit
    ipdetail                    VARCHAR(20),
    remarks                     VARCHAR(500),
    updated_ts                  TIMESTAMPTZ     DEFAULT CURRENT_TIMESTAMP   NOT NULL,

    -- Constraints
    CONSTRAINT test_frc_data_pk
        PRIMARY KEY (id),

    CONSTRAINT test_frc_uq_batch_caf
        UNIQUE (batch_date, caf_serial_no),

    CONSTRAINT test_frc_status_chk
        CHECK (process_status IN (
            'NEW','LOCKED','AUTH_PENDING','SUBMITTED','AWAITING_CB',
            'STATUS_CHECK','SUCCESS','FAILED','RETRY_PENDING',
            'PERM_FAILED','MISSED'
        )),

    CONSTRAINT test_frc_final_chk
        CHECK (final_status IS NULL OR final_status IN ('SUCCESS','FAILED')),

    CONSTRAINT test_frc_kyc_chk
        CHECK (kyc_mode IN ('EKYC','DKYC','SKYC')),

    CONSTRAINT test_frc_in_status_chk
        CHECK (in_status IN ('C','S','F')),

    CONSTRAINT test_frc_pyro_status_chk
        CHECK (pyro_status IN ('N','S','C','F')),

    CONSTRAINT test_frc_push_flag_chk
        CHECK (push_flag IN ('N','Y')),

    CONSTRAINT test_frc_bcd_upd_chk
        CHECK (bcd_updated IN ('N','Y')),

    CONSTRAINT test_frc_missed_chk
        CHECK (is_missed IN ('N','Y')),

    CONSTRAINT test_frc_retry_budget_chk
        CHECK (retry_count <= max_retries + 1)
);


-- Indexes
CREATE INDEX idx_tfrc_pickup
    ON caf_admin.test_frc_data
    (process_status, batch_date, next_retry_at, lock_until);

CREATE INDEX idx_tfrc_missed
    ON caf_admin.test_frc_data
    (batch_date, process_status, is_missed);

CREATE INDEX idx_tfrc_client_txn
    ON caf_admin.test_frc_data (client_txn_id);

CREATE INDEX idx_tfrc_pyro_trans
    ON caf_admin.test_frc_data (pyro_trans_id);

CREATE INDEX idx_tfrc_caf
    ON caf_admin.test_frc_data (caf_serial_no, batch_date);

CREATE INDEX idx_tfrc_gsmno
    ON caf_admin.test_frc_data (gsmno, process_status);

CREATE INDEX idx_tfrc_cb_eligible
    ON caf_admin.test_frc_data (status_check_eligible_at, process_status);

CREATE INDEX idx_tfrc_stale_lock
    ON caf_admin.test_frc_data (lock_until, process_status);

CREATE INDEX idx_tfrc_bcd_upd
    ON caf_admin.test_frc_data (bcd_updated, final_status);

-- error_log JSONB index for log querying
CREATE INDEX idx_tfrc_error_log
    ON caf_admin.test_frc_data USING GIN (error_log);


-- Trigger: timestamps and derived fields only
CREATE OR REPLACE FUNCTION caf_admin.trg_tfrc_auto_fn()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_ts := CURRENT_TIMESTAMP;

    IF TG_OP = 'INSERT' THEN
        NEW.created_at   := CURRENT_TIMESTAMP;
        NEW.scheduled_at := CURRENT_TIMESTAMP;
    END IF;

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

CREATE TRIGGER trg_tfrc_auto
BEFORE INSERT OR UPDATE ON caf_admin.test_frc_data
FOR EACH ROW EXECUTE FUNCTION caf_admin.trg_tfrc_auto_fn();


-- =============================================================================
-- TABLE 2: frc_txn_log
-- =============================================================================

CREATE TABLE caf_admin.frc_txn_log (

    -- Identity
    id                      UUID        DEFAULT gen_random_uuid()   NOT NULL,
    log_seq                 BIGSERIAL                               NOT NULL,

    -- Link to control table
    frc_request_id          UUID                                    NOT NULL,
    reqid                   BIGINT                                  NOT NULL,
    caf_serial_no           VARCHAR(30)                             NOT NULL,
    gsmno                   VARCHAR(10)                             NOT NULL,
    batch_date              DATE                                    NOT NULL,
    client_txn_id           VARCHAR(15),

    -- API interaction
    api_stage               VARCHAR(20)                             NOT NULL,
    api_endpoint            VARCHAR(200),
    http_method             VARCHAR(6),
    attempt_no              SMALLINT    DEFAULT 1                   NOT NULL,

    -- Request
    request_headers_masked  VARCHAR(500),
    request_body_masked     JSONB,

    -- Response
    response_http_code      SMALLINT,
    response_body           JSONB,
    pyro_status_code        INTEGER,
    pyro_status_text        VARCHAR(30),
    pyro_txn_id             BIGINT,

    -- Timing
    call_started_at         TIMESTAMPTZ                             NOT NULL,
    call_ended_at           TIMESTAMPTZ,
    duration_ms             INTEGER,

    -- Outcome
    is_success              VARCHAR(1)                              NOT NULL,
    is_perm_failure         VARCHAR(1)  DEFAULT 'N'                 NOT NULL,

    -- Error
    error_class             VARCHAR(200),
    error_detail            TEXT,

    -- Worker
    worker_id               VARCHAR(200),
    logged_at               TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP   NOT NULL,

    -- Constraints
    CONSTRAINT frc_txn_log_pk
        PRIMARY KEY (id),

    CONSTRAINT frc_txn_log_fk
        FOREIGN KEY (frc_request_id)
        REFERENCES caf_admin.test_frc_data (id),

    CONSTRAINT frc_txn_log_stage_chk
        CHECK (api_stage IN (
            'AUTH','REFRESH_TOKEN','ACTION_TOKEN',
            'RECHARGE','STATUS_CHECK','CALLBACK_RECV'
        )),

    CONSTRAINT frc_txn_log_success_chk
        CHECK (is_success IN ('Y','N')),

    CONSTRAINT frc_txn_log_perm_fail_chk
        CHECK (is_perm_failure IN ('Y','N'))
);


-- Indexes
CREATE INDEX idx_ftxnlog_request
    ON caf_admin.frc_txn_log
    (frc_request_id, api_stage, logged_at);

CREATE INDEX idx_ftxnlog_errors
    ON caf_admin.frc_txn_log
    (batch_date, is_success, pyro_status_code);

CREATE INDEX idx_ftxnlog_duration
    ON caf_admin.frc_txn_log (duration_ms, api_stage);

CREATE INDEX idx_ftxnlog_pyro_txn
    ON caf_admin.frc_txn_log (pyro_txn_id)
    WHERE api_stage IN ('CALLBACK_RECV','STATUS_CHECK');

CREATE INDEX idx_ftxnlog_gsmno
    ON caf_admin.frc_txn_log (gsmno, logged_at);

-- response_body and request_body JSONB indexes
CREATE INDEX idx_ftxnlog_response_body
    ON caf_admin.frc_txn_log USING GIN (response_body);

CREATE INDEX idx_ftxnlog_request_body
    ON caf_admin.frc_txn_log USING GIN (request_body_masked);