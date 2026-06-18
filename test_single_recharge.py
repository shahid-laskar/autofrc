"""
Single Pyro recharge test without database access.

This script:
  1. Authenticates with Pyro.
  2. Generates the action token through app.pyro_client.recharge().
  3. Sends exactly one recharge request.
  4. Checks transaction status if Pyro returns a transactionId.

It does not read or update Oracle/Postgres.

Usage:
    python test_single_recharge.py --dealer-msisdn 9XXXXXXXXX --dest-msisdn 9XXXXXXXXX --amount 10 --mpin 1234 --confirm-real-recharge

Optional:
    python test_single_recharge.py ... --client-txn-id TEST001 --status-wait-seconds 45 --show-tokens
"""

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone

from app.auth.token_manager import token_manager
from app.pyro_client import check_transaction_status, recharge


def _mask(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value)
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _print_json(label: str, data: dict) -> None:
    print(f"\n{label}")
    print(json.dumps(data, indent=2, default=str))


def _extract_transaction_id(response: dict) -> str | None:
    data = response.get("data")
    if isinstance(data, dict):
        txn_id = data.get("transactionId")
        return str(txn_id) if txn_id else None
    return None


async def run(args: argparse.Namespace) -> None:
    if not args.confirm_real_recharge:
        raise SystemExit(
            "Refusing to run recharge without --confirm-real-recharge. "
            "Use only approved test/UAT numbers or an approved live test case."
        )

    client_txn_id = args.client_txn_id or f"TEST{int(time.time())}"

    auth_ok = await token_manager.authenticate()
    if not auth_ok:
        raise SystemExit("Pyro authentication failed; recharge not attempted.")

    token_summary = {
        "sessionToken": token_manager.session_token if args.show_tokens else _mask(token_manager.session_token),
        "accessToken": token_manager.access_token if args.show_tokens else _mask(token_manager.access_token),
    }
    _print_json("AUTH TOKENS", token_summary)

    recharge_response = await recharge(
        dealer_msisdn=args.dealer_msisdn,
        dest_msisdn=args.dest_msisdn,
        amount=args.amount,
        client_txn_id=client_txn_id,
        mpin=args.mpin,
    )
    _print_json("RECHARGE RESPONSE", {
        "clientTxnId": client_txn_id,
        "requestedAt": datetime.now(timezone.utc).isoformat(),
        "response": recharge_response,
    })

    pyro_txn_id = _extract_transaction_id(recharge_response)
    if not pyro_txn_id:
        _print_json("TRANSACTION STATUS SKIPPED", {
            "reason": "Recharge response did not contain data.transactionId",
        })
        return

    if args.status_wait_seconds > 0:
        print(f"\nWaiting {args.status_wait_seconds} seconds before status check...")
        await asyncio.sleep(args.status_wait_seconds)

    status_response = await check_transaction_status(
        pyro_trans_id=pyro_txn_id,
        client_txn_id=client_txn_id,
    )
    _print_json("TRANSACTION STATUS RESPONSE", {
        "pyroTxnId": pyro_txn_id,
        "clientTxnId": client_txn_id,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "response": status_response,
    })


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authenticate, run one Pyro recharge, and check transaction status without DB access."
    )
    parser.add_argument("--dealer-msisdn", required=True, help="Dealer/source MSISDN.")
    parser.add_argument("--dest-msisdn", required=True, help="Destination subscriber MSISDN.")
    parser.add_argument("--amount", required=True, type=int, help="Recharge amount.")
    parser.add_argument("--mpin", required=True, help="Dealer MPIN value expected by Pyro.")
    parser.add_argument("--client-txn-id", help="Client transaction ID. Defaults to TEST<timestamp>.")
    parser.add_argument(
        "--status-wait-seconds",
        type=int,
        default=45,
        help="Seconds to wait before transaction-status call. Use 0 to check immediately.",
    )
    parser.add_argument(
        "--show-tokens",
        action="store_true",
        help="Print full session/access tokens instead of masked values.",
    )
    parser.add_argument(
        "--confirm-real-recharge",
        action="store_true",
        help="Required safety flag because this can trigger an actual recharge.",
    )
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
