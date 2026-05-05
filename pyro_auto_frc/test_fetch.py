"""
Read-only eligibility fetch test.

This tests the batch-population read path only:
  1. Fetch eligible records from Oracle CAF_ADMIN.BCD.
  2. Fetch matching FRC/vendor/plan data from Postgres.
  3. Print the rows that would be inserted into frc_pyro_request_data.

It does not create tables, call Pyro, or update Oracle/Postgres.

Usage:
    python test_fetch.py
    python test_fetch.py --limit 10
    python test_fetch.py --show-mpin
"""

import argparse
import json
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.extras

try:
    import oracledb
except ImportError:  # pragma: no cover - fallback for older deployments
    import cx_Oracle as oracledb

from app.config import settings
from app.encryption import encrypt


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _mask(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value)
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def fetch_oracle_eligible(limit: int) -> list[dict]:
    sql = """
        SELECT
            GSMNUMBER,
            CAF_SERIAL_NO,
            DE_CSCCODE,
            CIRCLE_CODE,
            HLR_FINAL_ACT_DATE
        FROM CAF_ADMIN.BCD
        WHERE ACTIVATION_STATUS       = 'C' 
          AND HLR_FINAL_ACT_DATE IS NOT NULL
          AND FRC_FLOW_STATUS    = :status_np
          AND FRC_REQID          IS NULL
          AND ROWNUM             <= :fetch_size
        ORDER BY HLR_FINAL_ACT_DATE ASC
    """

    conn = oracledb.connect(
        user=settings.oracle_user,
        password=settings.oracle_password,
        dsn=settings.oracle_dsn,
    )
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(sql, status_np="NP", fetch_size=limit)
        cols = [col[0] for col in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def fetch_postgres_frc_data(gsm_numbers: list[str]) -> list[dict]:
    if not gsm_numbers:
        return []

    sql = """
        SELECT
            cb.gsmnumber,
            cb.caf_serial_no,
            cb.de_csccode,
            cb.circle_code,
            cb.live_photo_time,
            cb.frc_plan_name,
            cb.frc_plan_code,
            cb.frc_category_code,
            cb.frc_ctopup_number,
            cb.frc_ctopup_number_mpin,
            cm.pos_unique_code   AS vendorid,
            cm.ctopupno          AS vendormsisdn,
            fp.frc_amount        AS frcamt
        FROM public.cos_bcd cb
        JOIN public.ctop_master cm
            ON cm.ctopupno = cb.frc_ctopup_number
        JOIN public.frc_plan_table fp
            ON fp.plan_code = cb.frc_plan_code
           AND (fp.circle_code = cb.circle_code::TEXT
                OR fp.circle_code = '9999')
           AND (fp.end_date IS NULL OR fp.end_date >= CURRENT_DATE)
        WHERE cb.gsmnumber = ANY(%s)
          AND cb.frc_plan_name          IS NOT NULL
          AND cb.frc_plan_code          IS NOT NULL
          AND cb.frc_category_code      IS NOT NULL
          AND cb.frc_ctopup_number      IS NOT NULL
          AND cb.frc_ctopup_number_mpin IS NOT NULL
    """

    conn = psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
    )
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (gsm_numbers,))
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def build_insert_preview(oracle_rows: list[dict], pg_rows: list[dict], show_mpin: bool) -> dict:
    oracle_by_gsm = {str(row["GSMNUMBER"]): row for row in oracle_rows}
    pg_by_gsm = {str(row["gsmnumber"]): row for row in pg_rows}

    previews = []
    skipped = []

    for oracle in oracle_rows:
        gsm = str(oracle["GSMNUMBER"])
        pg = pg_by_gsm.get(gsm)
        if not pg:
            skipped.append({
                "gsmnumber": gsm,
                "caf_serial_no": oracle["CAF_SERIAL_NO"],
                "reason": "No complete Postgres FRC/vendor/plan match",
            })
            continue

        raw_mpin = pg.get("frc_ctopup_number_mpin") or ""
        # encrypted_mpin = encrypt(raw_mpin, settings.pyro_secret_key)

        previews.append({
            "caf_serial_no": pg["caf_serial_no"],
            "gsmno": gsm,
            "csccode": pg.get("de_csccode") or oracle.get("DE_CSCCODE"),
            "circle_code": oracle.get("CIRCLE_CODE"),
            "edate": oracle.get("HLR_FINAL_ACT_DATE"),
            "reqdate": pg.get("live_photo_time"),
            "frc_plan_name": pg.get("frc_plan_name"),
            "frc_plan_code": pg.get("frc_plan_code"),
            "frc_category_code": pg.get("frc_category_code"),
            "frcamt": int(pg.get("frcamt", 0)),
            "ctopup_number": pg.get("frc_ctopup_number"),
            "vendormsisdn": pg.get("vendormsisdn"),
            "vendorid": pg.get("vendorid"),
            "mpin": raw_mpin if show_mpin else _mask(raw_mpin),
            "mpin_length": len(raw_mpin),
        })

    return {
        "oracle_fetched": len(oracle_rows),
        "pg_frc_eligible": len(pg_rows),
        "would_insert": len(previews),
        "skipped_no_pg_match": len(skipped),
        "rows": previews,
        "skipped": skipped,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read Oracle and Postgres eligibility data without updating either DB."
    )
    parser.add_argument("--limit", type=int, default=20, help="Maximum Oracle BCD rows to fetch.")
    parser.add_argument(
        "--show-mpin",
        action="store_true",
        help="Print encrypted MPIN values instead of masking them.",
    )
    args = parser.parse_args()

    oracle_rows = fetch_oracle_eligible(args.limit)
    gsm_numbers = [str(row["GSMNUMBER"]) for row in oracle_rows]
    pg_rows = fetch_postgres_frc_data(gsm_numbers)
    result = build_insert_preview(oracle_rows, pg_rows, args.show_mpin)

    print(json.dumps({
        "readonly": True,
        "updates_performed": False,
        "creates_tables": False,
        **result,
    }, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
