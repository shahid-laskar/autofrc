"""
app/processor.py
----------------
Recharge dispatch loop.

For each pending row (push_flag IN N/E):
  1. Call Pyro /recharge (encrypted body, decrypted response)
  2. statusCode 2002 -> push_flag='P', BCD -> 'W'
  3. Permanent/data error -> push_flag='F', BCD -> 'ID' or 'F'
  4. Transient error -> push_flag='E' (retry), BCD not updated yet

BCD writeback at each state:
  Submission OK   -> W   (waiting for callback)
  Data failure    -> ID  (5006/5007/5011/5012/5030/406)
  General failure -> F   (500/timeout/etc after retries exhausted)
"""

import asyncio
import json
import logging

from app.db.oracle import (
    BCD_STATUS_F,
    BCD_STATUS_ID,
    BCD_STATUS_W,
    INVALID_DATA_CODES,
    update_bcd_status,
)
from app.db.postgres import (
    FLAG_FAILED,
    FLAG_RETRY,
    async_fetch_pending_rows,
    async_mark_as_failed,
    async_mark_as_pushed,
)
from app.pyro_client import PERMANENT_FAILURE_CODES, recharge
from app.config import settings
from app.encryption import decrypt

logger = logging.getLogger(__name__)


async def process_pending_recharges(batch_size: int = 500) -> dict:
    """
    Main processing loop -- called by scheduler every 30 min
    and by POST /admin/trigger-recharge.
    """
    rows = await async_fetch_pending_rows(batch_size)
    if not rows:
        logger.info("Processor: no pending rows")
        return {"processed": 0, "registered": 0, "perm_failed": 0, "retryable": 0}

    registered = perm_failed = retryable = 0

    for row in rows:
        reqid         = row["reqid"]
        caf           = row["caf_serial_no"]
        gsmno         = row["gsmno"]
        dealer_msisdn = row["vendormsisdn"] or row["ctopup_number"]
        amount        = int(row["frcamt"])
        mpin          = decrypt(row["mpin"], settings.pyro_secret_key)  # already 3DES-encrypted in DB
        attempt       = int(row["retry_count"]) + 1
        client_txn_id = row.get("client_txn_id") or str(reqid).zfill(5)[:15]

        logger.info("Processing reqid=%s gsmno=%s amount=%s attempt=%s",
                    reqid, gsmno, amount, attempt)

        response = await recharge(
            dealer_msisdn=dealer_msisdn,
            dest_msisdn=gsmno,
            amount=amount,
            client_txn_id=client_txn_id,
            mpin=mpin,
        )

        status_code   = response.get("statusCode")
        response_text = json.dumps(response)
        inner         = response.get("data", {})

        if status_code == 2002:
            # Registered -- await callback
            pyro_trans_id = inner.get("transactionId")
            await async_mark_as_pushed(
                reqid, pyro_trans_id, response_text,
                f"dealerMsisdn={dealer_msisdn} destMsisdn={gsmno} amount={amount}",
                status_code,
            )
            # BCD: W = Waiting for callback
            await asyncio.to_thread(
                update_bcd_status, caf, reqid, BCD_STATUS_W,
                f"FRC submitted to Pyro. pyroTxnId={pyro_trans_id}"
            )
            logger.info("reqid=%s registered -- pyroTxnId=%s", reqid, pyro_trans_id)
            registered += 1

        elif status_code in PERMANENT_FAILURE_CODES:
            remarks = f"[{status_code}] {response.get('message', 'Permanent failure')}"
            await async_mark_as_failed(reqid, FLAG_FAILED, remarks,
                                       response_text, status_code)
            # BCD: ID for data errors, F for others
            bcd_status = (BCD_STATUS_ID if status_code in INVALID_DATA_CODES
                          else BCD_STATUS_F)
            await asyncio.to_thread(
                update_bcd_status, caf, reqid, bcd_status, remarks
            )
            logger.warning("reqid=%s PERMANENT [%s]: %s", reqid, bcd_status, remarks)
            perm_failed += 1

        else:
            # Transient -- retry on next scheduler run
            # BCD not updated yet; updated to F only after max retries exhausted
            remarks = f"[{status_code}] {response.get('message', 'Transient error')}"
            new_retry_count = attempt  # attempt = retry_count + 1 already
            await async_mark_as_failed(reqid, FLAG_RETRY, remarks,
                                       response_text, status_code)

            # If this was the last retry, update BCD to F
            if new_retry_count >= int(row["max_retries"]):
                await asyncio.to_thread(
                    update_bcd_status, caf, reqid, BCD_STATUS_F,
                    f"Max retries exhausted. Last error: {remarks}"
                )
                logger.warning("reqid=%s max retries exhausted -> BCD=F: %s",
                               reqid, remarks)
            else:
                logger.warning("reqid=%s TRANSIENT (retry %d/%d): %s",
                               reqid, new_retry_count, row["max_retries"], remarks)
            retryable += 1

    summary = {
        "processed":   len(rows),
        "registered":  registered,
        "perm_failed": perm_failed,
        "retryable":   retryable,
    }
    logger.info("Processor complete -- %s", summary)
    return summary
