"""
app/status_checker.py
---------------------
Fallback for rows stuck in push_flag='P' with no callback.
Runs every 5 minutes. Processes rows pushed 2-60 min ago.

BCD writeback at each outcome:
  2000 SUCCESS -> BCD: P (Processed)
  902  FAILED  -> BCD: F (Failed)
  901  NOT FOUND -> BCD stays W; retry next cycle (no BCD update)
  other -> BCD: F
"""

import asyncio
import json
import logging

from app.db.oracle import (
    BCD_STATUS_F,
    BCD_STATUS_NR,
    BCD_STATUS_P,
    update_bcd_status,
)
from app.db.postgres import (
    FLAG_FAILED,
    FLAG_RETRY,
    async_fetch_pushed_rows_for_status_check,
    async_mark_as_failed,
    async_mark_as_success,
    async_update_status_check_attempt,
)
from app.pyro_client import check_transaction_status

logger = logging.getLogger(__name__)


async def run_status_checks() -> dict:
    """Check status of pushed-but-no-callback rows."""
    rows = await async_fetch_pushed_rows_for_status_check()
    if not rows:
        logger.info("Status checker: no rows pending")
        return {"checked": 0, "success": 0, "failed": 0, "retry": 0}

    logger.info("Status checker: checking %d row(s)", len(rows))
    success = failed = retry = 0

    for row in rows:
        reqid         = row["reqid"]
        pyro_trans_id = row["pyro_trans_id"]
        caf           = row["caf_serial_no"]
        gsmno         = row["gsmno"]
        batch_date    = row["batch_date"]

        await async_update_status_check_attempt(reqid)

        # First time status check is initiated -> BCD: NR (No Response)
        if row.get("status_check_count", 0) == 0:
            await asyncio.to_thread(
                update_bcd_status, caf, reqid, BCD_STATUS_NR,
                f"No callback received. Initiating status check. pyroTxnId={pyro_trans_id}"
            )

        response = await check_transaction_status(
            reqid=reqid,
            caf_serial_no=caf,
            gsmno=gsmno,
            batch_date=batch_date,
            pyro_trans_id=str(pyro_trans_id),
            attempt_no=row["status_check_count"] + 1,
        )

        status_code   = response.get("statusCode")
        response_text = json.dumps(response)
        inner         = response.get("data", {})

        if status_code == 2000:
            balance_before = inner.get("dealerBalanceBefore", 0.0)
            balance_after = inner.get("dealerBalanceAfter", 0.0)
            await async_mark_as_success(reqid, response_text,balance_before, balance_after, status_code)
            await asyncio.to_thread(
                update_bcd_status, caf, reqid, BCD_STATUS_P,
                f"Recharge successful via status check. pyroTxnId={pyro_trans_id}"
            )
            logger.info("reqid=%s STATUS CHECK SUCCESS", reqid)
            success += 1

        elif status_code == 902:
            remarks = f"[902] Transaction failed on Pyro"
            await async_mark_as_failed(reqid, FLAG_FAILED, remarks,
                                       response_text, status_code)
            await asyncio.to_thread(
                update_bcd_status, caf, reqid, BCD_STATUS_F,
                f"Transaction failed on Pyro. pyroTxnId={pyro_trans_id}"
            )
            logger.warning("reqid=%s STATUS CHECK: FAILED (902)", reqid)
            failed += 1

        elif status_code == 901:
            # Not found yet -- keep waiting, BCD stays NR
            await async_update_status_check_attempt(reqid)
            # await async_mark_as_failed(
            #     reqid, FLAG_RETRY,
            #     "[901] Not found on Pyro yet -- will retry",
            #     response_text, status_code,
            # )
            logger.warning("reqid=%s STATUS CHECK: not found (901) -- retry", reqid)
            retry += 1

        else:
            remarks = f"[{status_code}] {response.get('message', 'Unknown')}"
            await async_mark_as_failed(reqid, FLAG_RETRY, remarks,
                                       response_text, status_code)
            await asyncio.to_thread(
                update_bcd_status, caf, reqid, BCD_STATUS_F, remarks
            )
            logger.warning("reqid=%s STATUS CHECK unknown: %s", reqid, remarks)
            retry += 1

    summary = {"checked": len(rows), "success": success,
               "failed": failed, "retry": retry}
    logger.info("Status checker complete -- %s", summary)
    return summary