"""
app/batch/populator.py
----------------------
Daily batch job populating frc_pyro_request_data.

Flow:
  1. Fetch eligible BCD records from Oracle
     (HLR_FINAL_ACT_DATE NOT NULL, FRC_FLOW_STATUS='NP', FRC_REQID IS NULL)
  2. Query Postgres cos_bcd for those GSMs joined with ctop_master + frc_plan_table
  3. Encrypt MPIN, build insert rows
  4. Bulk insert into frc_pyro_request_data (ON CONFLICT DO NOTHING)
  5. Write back Oracle BCD: FRC_FLOW_STATUS='RQ', FRC_REQID=reqid
     -- THIS is the primary idempotency guard for future batch runs

Idempotency strategy:
  Primary   : Oracle BCD FRC_FLOW_STATUS='NP' filter -- only unprocessed rows fetched
  Secondary : Postgres UNIQUE(batch_date, caf_serial_no) -- DB-level safety net
  After insert: BCD immediately updated to 'RQ' so next batch skips this record
  No Postgres flags needed for idempotency -- BCD writeback handles it
"""

import logging
from datetime import date
from typing import List

from app.config import settings
from app.db.oracle import (
    BCD_STATUS_RQ,
    batch_writeback_bcd_rq,
    fetch_eligible_bcd_records,
)
from app.db.postgres import bulk_insert_frc_requests, fetch_cos_bcd_for_gsms
from app.encryption import encrypt

logger = logging.getLogger(__name__)


def _encrypt_mpin(mpin: str) -> str:
    return encrypt(mpin, settings.pyro_secret_key)


def run_batch_population() -> dict:
    """
    Main entry point. Called by scheduler daily and via admin trigger.
    Returns summary dict.
    """
    today = date.today().isoformat()
    logger.info("Batch population started for %s", today)

    summary = {
        "batch_date":       today,
        "oracle_fetched":   0,
        "pg_frc_eligible":  0,
        "skipped_no_frc":   0,
        "skipped_no_ctop":  0,
        "skipped_no_plan":  0,
        "skipped_mpin_err": 0,
        "inserted":         0,
        "bcd_rq_updated":   0,
        "errors":           0,
    }

    # Step 1: Oracle BCD -- eligible records
    try:
        bcd_records = fetch_eligible_bcd_records(
            fetch_size=settings.oracle_batch_fetch_size
        )
    except Exception as exc:
        logger.error("Batch: Oracle fetch failed -- %s", exc)
        summary["errors"] += 1
        return summary

    if not bcd_records:
        logger.info("Batch: no eligible BCD records")
        return summary

    summary["oracle_fetched"] = len(bcd_records)
    gsm_list   = [r["GSMNUMBER"]     for r in bcd_records]
    bcd_by_gsm = {r["GSMNUMBER"]: r  for r in bcd_records}

    # Step 2: Postgres cos_bcd + ctop_master + frc_plan_table join
    try:
        pg_rows = fetch_cos_bcd_for_gsms(gsm_list)
    except Exception as exc:
        logger.error("Batch: Postgres cos_bcd fetch failed -- %s", exc)
        summary["errors"] += 1
        return summary

    summary["pg_frc_eligible"] = len(pg_rows)
    pg_gsms = {r["gsmnumber"] for r in pg_rows}
    summary["skipped_no_frc"] = len(gsm_list) - len(pg_gsms)

    if not pg_rows:
        logger.info("Batch: no GSMs with complete FRC data in Postgres")
        return summary

    # Step 3: Build insert rows
    rows_to_insert: List[dict] = []

    for pg in pg_rows:
        gsm    = pg["gsmnumber"]
        caf    = pg["caf_serial_no"]
        oracle = bcd_by_gsm.get(gsm)

        if not oracle:
            logger.warning("Batch: no Oracle record for GSM=%s -- skipping", gsm)
            summary["errors"] += 1
            continue

        if not pg.get("vendorid") or not pg.get("vendormsisdn"):
            logger.warning("Batch: no ctop_master match for CAF=%s GSM=%s", caf, gsm)
            summary["skipped_no_ctop"] += 1
            continue

        if pg.get("frcamt") is None:
            logger.warning("Batch: no frc_plan_table match for CAF=%s GSM=%s", caf, gsm)
            summary["skipped_no_plan"] += 1
            continue

        raw_mpin = pg.get("frc_ctopup_number_mpin", "")
        try:
            encrypted_mpin = _encrypt_mpin(raw_mpin)
            mpin_length    = len(raw_mpin)
        except Exception as exc:
            logger.error("Batch: MPIN encrypt failed CAF=%s -- %s", caf, exc)
            summary["skipped_mpin_err"] += 1
            continue

        rows_to_insert.append({
            "caf_serial_no":     caf,
            "gsmno":             gsm,
            "csccode":           pg.get("de_csccode") or oracle.get("DE_CSCCODE"),
            "circle_code":       oracle.get("CIRCLE_CODE"),
            "edate":             oracle.get("HLR_FINAL_ACT_DATE"),
            "reqdate":           pg.get("live_photo_time"),
            "frc_plan_name":     pg.get("frc_plan_name"),
            "frc_plan_code":     pg.get("frc_plan_code"),
            "frc_category_code": pg.get("frc_category_code"),
            "frcamt":            int(pg.get("frcamt", 0)),
            "ctopup_number":     pg.get("frc_ctopup_number"),
            "vendormsisdn":      pg.get("vendormsisdn"),
            "vendorid":          pg.get("vendorid"),
            "mpin":              encrypted_mpin,
            "mpin_length":       mpin_length,
        })

    if not rows_to_insert:
        logger.info("Batch: no rows to insert after validation")
        return summary

    # Step 4: Bulk insert into Postgres (returns reqid per inserted row)
    try:
        inserted_pairs = bulk_insert_frc_requests(rows_to_insert)
        summary["inserted"] = len(inserted_pairs)
    except Exception as exc:
        logger.error("Batch: bulk insert failed -- %s", exc)
        summary["errors"] += 1
        return summary

    # Step 5: Write back Oracle BCD -> RQ (primary idempotency guard)
    # Must happen after successful Postgres insert.
    # If this fails, the rows exist in Postgres but BCD still shows NP.
    # Next batch run will attempt re-insert but Postgres UNIQUE constraint
    # will silently skip them (ON CONFLICT DO NOTHING).
    if inserted_pairs:
        try:
            updated = batch_writeback_bcd_rq(inserted_pairs)
            summary["bcd_rq_updated"] = updated
        except Exception as exc:
            logger.error(
                "Batch: BCD writeback failed for %d rows -- %s. "
                "Postgres rows are inserted. Next batch will skip via UNIQUE constraint.",
                len(inserted_pairs), exc
            )
            summary["errors"] += 1

    logger.info(
        "Batch population complete: oracle=%d pg_eligible=%d "
        "inserted=%d bcd_updated=%d "
        "skip_no_frc=%d skip_no_ctop=%d skip_no_plan=%d skip_mpin=%d errors=%d",
        summary["oracle_fetched"], summary["pg_frc_eligible"],
        summary["inserted"], summary["bcd_rq_updated"],
        summary["skipped_no_frc"], summary["skipped_no_ctop"],
        summary["skipped_no_plan"], summary["skipped_mpin_err"],
        summary["errors"],
    )
    return summary