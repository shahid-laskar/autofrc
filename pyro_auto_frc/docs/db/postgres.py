"""
app/db/postgres.py
------------------
Postgres connection pool (psycopg2) and all DB operations.

Handles:
  READ  : cos_bcd, ctop_master, frc_plan_table (source data for batch population)
  WRITE : frc_pyro_request_data (recharge state machine)
  WRITE : frc_txn_log (immutable API call audit)

All sync functions are wrapped with asyncio.to_thread() in the async
wrappers at the bottom so they don't block the FastAPI event loop.

Table: cos_bcd (EKYC KYC data — Postgres copy)
  Key fields: gsmnumber, caf_serial_no, frc_plan_name, frc_plan_code,
              frc_category_code, frc_ctopup_number, frc_ctopup_number_mpin,
              live_photo_time

Table: ctop_master (vendor/ctopup master data)
  Key fields: ctopupno (join key), pos_unique_code (VENDORID)

Table: frc_plan_table (FRC plan definitions)
  Key fields: plan_code (join key), frc_amount, plan_name, category_code
"""

import asyncio
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator, List, Optional

import psycopg2
import psycopg2.pool
import psycopg2.extras

from app.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None

# ── PUSH_FLAG state constants ──────────────────────────────────────────────────
FLAG_PENDING = "N"   # default — not yet pushed to Pyro
FLAG_PUSHED  = "P"   # submitted to Pyro, awaiting callback
FLAG_SUCCESS = "Y"   # final success confirmed
FLAG_FAILED  = "F"   # permanent failure — manual fix needed
FLAG_RETRY   = "E"   # transient error — auto-retry on next scheduler run

# Permanent failure codes from Pyro — do not retry
PERMANENT_FAILURE_CODES = {405, 406, 5001, 5002, 5006, 5007, 5011, 5012, 5030}


# ── Pool lifecycle ─────────────────────────────────────────────────────────────

def init_pg_pool() -> None:
    """Create psycopg2 connection pool. Called once on startup."""
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=settings.pg_min_conn,
        maxconn=settings.pg_max_conn,
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
    )
    logger.info("Postgres connection pool initialised (min=%d max=%d)",
                settings.pg_min_conn, settings.pg_max_conn)


def close_pg_pool() -> None:
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("Postgres connection pool closed")


@contextmanager
def get_pg_conn() -> Generator:
    """Acquire a connection from pool; commit on success, rollback on error."""
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ── Source data queries (batch population) ────────────────────────────────────

def fetch_cos_bcd_for_gsms(gsm_numbers: List[str]) -> List[dict]:
    """
    Fetch FRC indicator fields from cos_bcd for a list of GSM numbers.

    Only returns rows where ALL FIVE FRC indicator fields are non-null
    — this is the indicator that FRC recharge is required.

    Join with ctop_master to get VENDORID (pos_unique_code) and
    with frc_plan_table to get frc_amount.

    Returns:
        List of dicts with all fields needed for frc_pyro_request_data insert.
    """
    if not gsm_numbers:
        return []

    sql = """
        SELECT
            cb.gsmnumber,
            cb.caf_serial_no,
            cb.de_csccode,
            cb.circle_code,
            cb.hlr_final_act_date,
            cb.live_photo_time,
            cb.frc_plan_name,
            cb.frc_plan_code,
            cb.frc_category_code,
            cb.frc_ctopup_number,
            cb.frc_ctopup_number_mpin,
            cm.pos_unique_code          AS vendorid,
            cm.ctopupno                 AS vendormsisdn,
            fp.frc_amount               AS frcamt,
            fp.plan_name                AS frc_plan_name_from_table
        FROM public.cos_bcd cb

        -- Join ctop_master on the FRC ctopup number to get vendor details
        JOIN public.ctop_master cm
            ON cm.ctopupno = cb.frc_ctopup_number

        -- Join frc_plan_table to get the denomination
        -- circle_code '9999' = applies to all circles
        JOIN public.frc_plan_table fp
            ON fp.plan_code = cb.frc_plan_code
           AND (fp.circle_code = cb.circle_code::TEXT
                OR fp.circle_code = '9999')
           AND (fp.end_date IS NULL OR fp.end_date >= CURRENT_DATE)

        WHERE cb.gsmnumber = ANY(%s)
          -- All five FRC indicator fields must be non-null
          AND cb.frc_plan_name          IS NOT NULL
          AND cb.frc_plan_code          IS NOT NULL
          AND cb.frc_category_code      IS NOT NULL
          AND cb.frc_ctopup_number      IS NOT NULL
          AND cb.frc_ctopup_number_mpin IS NOT NULL
    """
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (gsm_numbers,))
            rows = [dict(r) for r in cur.fetchall()]

    logger.debug("cos_bcd join: %d/%d GSMs have complete FRC data",
                 len(rows), len(gsm_numbers))
    return rows


def get_already_inserted_cafs(caf_serials: List[str], batch_date: str) -> set:
    """
    Return caf_serial_no values already in frc_pyro_request_data for batch_date.
    Used to skip duplicates during batch population.
    """
    if not caf_serials:
        return set()

    sql = """
        SELECT caf_serial_no
        FROM public.frc_pyro_request_data
        WHERE batch_date = %s
          AND caf_serial_no = ANY(%s)
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (batch_date, caf_serials))
            return {row[0] for row in cur.fetchall()}


def bulk_insert_frc_requests(rows: List[dict]) -> int:
    """
    Bulk insert rows into frc_pyro_request_data.
    Uses ON CONFLICT DO NOTHING for idempotency (safe to re-run).
    Returns number of rows actually inserted.
    """
    if not rows:
        return 0

    sql = """
        INSERT INTO public.frc_pyro_request_data (
            caf_serial_no, gsmno, csccode, circle_code, kyc_mode,
            edate, reqdate,
            frc_plan_name, frc_plan_code, frc_category_code, frcamt,
            ctopup_number, vendormsisdn, vendorid,
            mpin, mpin_length,
            in_status, pyro_status, push_flag,
            batch_date, created_at
        ) VALUES (
            %(caf_serial_no)s, %(gsmno)s, %(csccode)s, %(circle_code)s, %(kyc_mode)s,
            %(edate)s, %(reqdate)s,
            %(frc_plan_name)s, %(frc_plan_code)s, %(frc_category_code)s, %(frcamt)s,
            %(ctopup_number)s, %(vendormsisdn)s, %(vendorid)s,
            %(mpin)s, %(mpin_length)s,
            'C', 'N', 'N',
            CURRENT_DATE, CURRENT_TIMESTAMP
        )
        ON CONFLICT (batch_date, caf_serial_no) DO NOTHING
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=100)
            inserted = cur.rowcount
    return inserted


# ── Recharge state machine queries ────────────────────────────────────────────

def fetch_pending_rows(batch_size: int = 500) -> List[dict]:
    """
    Fetch rows ready for Pyro recharge submission.
    Picks up both fresh rows (N) and retry rows (E).
    Orders oldest first for fairness.
    """
    sql = """
        SELECT
            reqid, caf_serial_no, gsmno,
            vendormsisdn, vendorid, frcamt, mpin, mpin_length,
            push_flag, retry_count, max_retries, kyc_mode,
            client_txn_id, ctopup_number, batch_date
        FROM public.frc_pyro_request_data
        WHERE in_status  = 'C'
          AND push_flag  IN ('N', 'E')
          AND retry_count <= max_retries
        ORDER BY created_at ASC
        LIMIT %s
    """
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (batch_size,))
            rows = [dict(r) for r in cur.fetchall()]

    logger.info("Fetched %d pending rows for recharge processing", len(rows))
    return rows


def mark_as_pushed(reqid: int, pyro_trans_id: int, response_text: str,
                   msg2pyro: str, initial_statuscode: int) -> None:
    """Set push_flag='P' after successfully submitting to Pyro."""
    sql = """
        UPDATE public.frc_pyro_request_data
        SET
            push_flag             = 'P',
            push_date             = CURRENT_TIMESTAMP,
            push_remarks          = 'Submitted to Pyro — awaiting callback',
            pyro_trans_id         = %s,
            pyro_initial_statuscode = %s,
            submitted_at          = CURRENT_TIMESTAMP,
            msg2pyro              = %s,
            msg_afterreq          = %s,
            pyro_status           = 'REG',
            client_txn_id         = %s
        WHERE reqid = %s
    """
    client_txn_id = str(reqid).zfill(5)[:15]
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (pyro_trans_id, initial_statuscode,
                              msg2pyro, response_text,
                              client_txn_id, reqid))


def mark_as_success(reqid: int, response_text: str,
                    balance_after: float, final_statuscode: int) -> None:
    """Set push_flag='Y' after confirmed success from callback or status check."""
    sql = """
        UPDATE public.frc_pyro_request_data
        SET
            push_flag             = 'Y',
            final_status          = 'SUCCESS',
            pyro_status           = 'SUC',
            pyro_final_statuscode = %s,
            completed_at          = CURRENT_TIMESTAMP,
            callback_received_at  = CURRENT_TIMESTAMP,
            dealer_bal_after      = %s,
            msg_aftertr           = %s,
            replyrecvd_date       = CURRENT_TIMESTAMP,
            push_remarks          = 'Recharge successful'
        WHERE reqid = %s
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (final_statuscode, balance_after, response_text, reqid))


def mark_as_failed(reqid: int, push_flag: str, remarks: str,
                   response_text: Optional[str] = None,
                   final_statuscode: Optional[int] = None) -> None:
    """
    Set push_flag to FLAG_FAILED ('F') or FLAG_RETRY ('E').
    Also increments retry_count for retry cases.
    """
    is_permanent = push_flag == FLAG_FAILED
    sql = """
        UPDATE public.frc_pyro_request_data
        SET
            push_flag             = %s,
            push_remarks          = %s,
            last_error_msg        = %s,
            msg_afterreq          = COALESCE(%s, msg_afterreq),
            retry_count           = CASE WHEN %s THEN retry_count ELSE retry_count + 1 END,
            final_status          = CASE WHEN %s THEN 'FAILED' ELSE final_status END,
            pyro_final_statuscode = COALESCE(%s, pyro_final_statuscode),
            completed_at          = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE completed_at END,
            pyro_status           = CASE WHEN %s THEN 'FAL' ELSE pyro_status END
        WHERE reqid = %s
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                push_flag,
                remarks[:200],
                remarks[:500],
                response_text,
                is_permanent,   # don't increment if permanent
                is_permanent, final_statuscode, is_permanent, is_permanent,
                reqid,
            ))


def fetch_pushed_rows_for_status_check() -> List[dict]:
    """
    Fetch rows submitted to Pyro but with no callback received yet.
    Time window: pushed more than 2 minutes ago, less than 60 minutes ago.
    """
    sql = """
        SELECT
            reqid, pyro_trans_id, client_txn_id, gsmno, caf_serial_no
        FROM public.frc_pyro_request_data
        WHERE push_flag = 'P'
          AND (
              status_check_eligible_at IS NOT NULL
              AND status_check_eligible_at <= CURRENT_TIMESTAMP
          )
          AND push_date >= CURRENT_TIMESTAMP - INTERVAL '60 minutes'
          AND push_date <= CURRENT_TIMESTAMP - INTERVAL '2 minutes'
        ORDER BY push_date ASC
    """
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def update_status_check_attempt(reqid: int) -> None:
    """Increment status check counter and record timestamp."""
    sql = """
        UPDATE public.frc_pyro_request_data
        SET
            status_check_count   = status_check_count + 1,
            last_status_check_at = CURRENT_TIMESTAMP
        WHERE reqid = %s
    """
    with get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (reqid,))


def find_row_by_pyro_trans_id(pyro_trans_id: int) -> Optional[dict]:
    """Look up a row by PYRO_TRANS_ID — used by callback handler."""
    sql = """
        SELECT reqid, caf_serial_no, push_flag
        FROM public.frc_pyro_request_data
        WHERE pyro_trans_id = %s
    """
    with get_pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (pyro_trans_id,))
            row = cur.fetchone()
            return dict(row) if row else None


# ── Transaction log (audit) ───────────────────────────────────────────────────

def insert_txn_log(
    frc_reqid: int,
    caf_serial_no: str,
    gsmno: str,
    batch_date,
    client_txn_id: Optional[str],
    api_stage: str,
    api_endpoint: Optional[str],
    http_method: Optional[str],
    attempt_no: int,
    request_headers: Optional[str],
    request_body: Optional[str],
    response_http_code: Optional[int],
    response_body: Optional[str],
    pyro_status_code: Optional[int],
    pyro_status_text: Optional[str],
    pyro_txn_id: Optional[int],
    call_started_at: datetime,
    call_ended_at: Optional[datetime],
    duration_ms: Optional[int],
    is_success: str,
    is_perm_failure: str = "N",
    error_class: Optional[str] = None,
    error_detail: Optional[str] = None,
) -> None:
    """Insert one row into frc_txn_log. Never raises — log failures are non-fatal."""
    sql = """
        INSERT INTO public.frc_txn_log (
            frc_reqid, caf_serial_no, gsmno, batch_date, client_txn_id,
            api_stage, api_endpoint, http_method, attempt_no,
            request_headers, request_body,
            response_http_code, response_body,
            pyro_status_code, pyro_status_text, pyro_txn_id,
            call_started_at, call_ended_at, duration_ms,
            is_success, is_perm_failure, error_class, error_detail
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s
        )
    """
    try:
        with get_pg_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    frc_reqid, caf_serial_no, gsmno, batch_date, client_txn_id,
                    api_stage, api_endpoint, http_method, attempt_no,
                    request_headers, request_body,
                    response_http_code, response_body,
                    pyro_status_code, pyro_status_text, pyro_txn_id,
                    call_started_at, call_ended_at, duration_ms,
                    is_success, is_perm_failure, error_class, error_detail,
                ))
    except Exception as exc:
        logger.error("Failed to insert txn log for reqid=%s stage=%s: %s",
                     frc_reqid, api_stage, exc)


# ── Async wrappers (thread-pool) ──────────────────────────────────────────────

async def async_fetch_pending_rows(batch_size: int) -> List[dict]:
    return await asyncio.to_thread(fetch_pending_rows, batch_size)

async def async_mark_as_pushed(reqid, pyro_trans_id, response_text,
                                msg2pyro, initial_statuscode):
    await asyncio.to_thread(mark_as_pushed, reqid, pyro_trans_id,
                            response_text, msg2pyro, initial_statuscode)

async def async_mark_as_success(reqid, response_text, balance_after, final_statuscode):
    await asyncio.to_thread(mark_as_success, reqid, response_text,
                            balance_after, final_statuscode)

async def async_mark_as_failed(reqid, push_flag, remarks,
                                response_text=None, final_statuscode=None):
    await asyncio.to_thread(mark_as_failed, reqid, push_flag, remarks,
                            response_text, final_statuscode)

async def async_find_row_by_pyro_trans_id(pyro_trans_id: int):
    return await asyncio.to_thread(find_row_by_pyro_trans_id, pyro_trans_id)

async def async_fetch_pushed_rows_for_status_check():
    return await asyncio.to_thread(fetch_pushed_rows_for_status_check)

async def async_update_status_check_attempt(reqid: int):
    await asyncio.to_thread(update_status_check_attempt, reqid)

async def async_insert_txn_log(*args, **kwargs):
    await asyncio.to_thread(insert_txn_log, *args, **kwargs)
