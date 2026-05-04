"""
app/db/oracle.py
----------------
Oracle connection pool (cx_Oracle) for reading Oracle BCD table.

Purpose: Fetch activated BCD records eligible for FRC recharge.
Used ONLY by the batch populator — the recharge service itself
uses Postgres exclusively.

BCD eligibility criteria:
  
  HLR_FINAL_ACT_DATE IS NOT NULL   (HLR activation complete)  
  KYC_MODE           = 'EKYC'     default it to 'EKYC' as bcd table has null entries for kyc_mode (only EKYC has FRC fields in Postgres cos_bcd)
"""

import logging
from contextlib import contextmanager
from typing import Generator, List, Optional

import cx_Oracle

from app.config import settings

logger = logging.getLogger(__name__)

_pool: Optional[cx_Oracle.SessionPool] = None


def init_oracle_pool() -> None:
    """Create cx_Oracle connection pool. Called once on startup."""
    global _pool
    _pool = cx_Oracle.SessionPool(
        user=settings.oracle_user,
        password=settings.oracle_password,
        dsn=settings.oracle_dsn,
        min=1,
        max=5,
        increment=1,
        encoding="UTF-8",
    )
    logger.info("Oracle connection pool initialised (BCD access, min=1 max=5)")


def close_oracle_pool() -> None:
    global _pool
    if _pool:
        _pool.close()
        _pool = None
        logger.info("Oracle connection pool closed")


@contextmanager
def get_oracle_conn() -> Generator[cx_Oracle.Connection, None, None]:
    conn = _pool.acquire()
    try:
        yield conn
    finally:
        _pool.release(conn)


def fetch_eligible_bcd_records(fetch_size: int = 500) -> List[dict]:
    """
    Fetch BCD records eligible for FRC recharge from Oracle.

    Returns list of dicts with fields needed to populate frc_pyro_request_data.
    KYC_MODE is fixed to 'EKYC' because only cos_bcd (EKYC) has frc_ fields
    in Postgres. DKYC/SKYC will be added when those tables get frc_ columns.

    Fields returned:
      GSMNUMBER         → gsmno
      CAF_SERIAL_NO     → caf_serial_no
      DE_CSCCODE        → csccode
      CIRCLE_CODE       → circle_code
      KYC_MODE          → kyc_mode
      HLR_FINAL_ACT_DATE → edate
    """
    # will be used if we need to filter by FRC eligibility criteria in Oracle instead of Postgres.
    # sql = """
    #     SELECT
    #         GSMNUMBER,
    #         CAF_SERIAL_NO,
    #         DE_CSCCODE,
    #         CIRCLE_CODE,
    #         KYC_MODE,
    #         HLR_FINAL_ACT_DATE
    #     FROM CAF_ADMIN.BCD
    #     WHERE ACTIVATION_STATUS  = 'A'
    #       AND HLR_FINAL_ACT_DATE IS NOT NULL
    #       AND FRC_FLOW_STATUS    = 'NP'
    #       AND FRC_REQID          IS NULL
    #       AND KYC_MODE           = 'EKYC'
    #       AND ROWNUM             <= :fetch_size
    #     ORDER BY HLR_FINAL_ACT_DATE ASC
    # """

    sql = """
    SELECT
        GSMNUMBER,
        CAF_SERIAL_NO,
        DE_CSCCODE,
        CIRCLE_CODE,
        KYC_MODE,
        HLR_FINAL_ACT_DATE
    FROM CAF_ADMIN.BCD
    WHERE HLR_FINAL_ACT_DATE  IS NOT NULL
      AND ROWNUM               <= :fetch_size
    ORDER BY HLR_FINAL_ACT_DATE ASC
"""
    with get_oracle_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, fetch_size=fetch_size)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    logger.info("Oracle BCD: fetched %d eligible records for FRC", len(rows))
    return rows
