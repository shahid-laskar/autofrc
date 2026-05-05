"""
app/pyro_client.py
------------------
HTTP client for all outbound Pyro API calls.

Encryption summary (confirmed via Postman testing):
  - REQUEST bodies  : encrypted (3DES-ECB, Base64 out)   ← encrypt() before POST
  - RESPONSE bodies : plain JSON in current Pyro environment; encrypted fallback supported
  - CALLBACK bodies : plain JSON posted by Pyro           ← no decryption needed

This applies to:
  - POST /epin-vendor-api/recharge          (request encrypted, response parsed JSON-first)
  - POST /epin-vendor-api/transaction-status (request encrypted, response parsed JSON-first)
  - POST /auth-api/authentication            (request encrypted, response parsed JSON-first)
  - GET  /auth-api/refresh-access-token     (no body, response plain JSON)
  - GET  /auth-api/generate-action-token    (no body, response plain JSON)
"""

import json
import logging

import httpx

from app.config import settings
from app.encryption import decrypt, encrypt
from app.auth.token_manager import token_manager

logger = logging.getLogger(__name__)

STATUS_SUCCESS    = 2000
STATUS_REGISTERED = 2002

# Permanent failures — manual correction needed, do NOT auto-retry
PERMANENT_FAILURE_CODES = {405, 406, 5001, 5002, 5006, 5007, 5011, 5012, 5030}

# Transient failures — retry on next scheduler run
TRANSIENT_FAILURE_CODES = {500, 505, 5000}


def classify_failure(status_code: int) -> str:
    """Returns 'F' for permanent failure, 'E' for transient (retry)."""
    return "F" if status_code in PERMANENT_FAILURE_CODES else "E"


def _parse_pyro_response(resp: httpx.Response, label: str) -> dict:
    """
    Parse a Pyro API response.

    The current Pyro environment returns plain JSON. Encrypted Base64 parsing is
    kept as a fallback for environments that still use encrypted responses.
    """
    raw = resp.text.strip()
    logger.debug("%s raw response: %s", label, raw[:200])

    try:
        return resp.json()
    except Exception as json_err:
        logger.debug("%s: plain JSON parse failed (%s); trying encrypted response", label, json_err)

    try:
        decrypted = decrypt(raw, settings.pyro_secret_key)
        return json.loads(decrypted)
    except Exception as dec_err:
        logger.error("%s: both plain JSON and decrypt parse failed. Raw: %s", label, raw[:300])
        return {"statusCode": -1, "status": "ERROR", "message": f"Response parse failed: {dec_err}"}


async def recharge(
    dealer_msisdn: str,
    dest_msisdn: str,
    amount: int,
    client_txn_id: str,
    mpin: str,
) -> dict:
    """
    POST /epin-vendor-api/recharge

    Request  : JSON body encrypted with 3DES-ECB → sent as raw text
    Response : 3DES-ECB encrypted Base64 string  → decrypt → parse JSON

    On statusCode 2002: registered — final result comes via callback.
    On failure: statusCode indicates the reason (see PERMANENT_FAILURE_CODES).
    """
    action_token = await token_manager.get_action_token()
    if not action_token:
        return {"statusCode": -1, "status": "ERROR", "message": "Failed to generate action token"}

    body = {
        "dealerMsisdn": dealer_msisdn,
        "destMsisdn":   dest_msisdn,
        "amount":       amount,
        "clientTxnId":  str(client_txn_id),
        "mpin":         mpin,
    }
    encrypted_body = encrypt(json.dumps(body), settings.pyro_secret_key)

    url = f"{settings.pyro_base_url}/epin-vendor-api/recharge"
    headers = {
        "apiKey":       settings.pyro_api_key,
        "sessionToken": token_manager.session_token,
        "accessToken":  token_manager.access_token,
        "actionToken":  action_token,
    }

    try:
        async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
            resp = await client.post(url, headers=headers, content=encrypted_body)
        
        data = _parse_pyro_response(resp, f"recharge clientTxnId={client_txn_id}")

        logger.info("Recharge clientTxnId=%s: statusCode=%s", client_txn_id, data.get("statusCode"))
        return data

    except httpx.TimeoutException:
        logger.error("Recharge timed out for clientTxnId=%s", client_txn_id)
        return {"statusCode": -1, "status": "ERROR", "message": "Request timed out"}

    except Exception as exc:
        logger.error("Recharge error for clientTxnId=%s: %s", client_txn_id, exc)
        return {"statusCode": -1, "status": "ERROR", "message": str(exc)}


async def check_transaction_status(pyro_trans_id: str, client_txn_id: str) -> dict:
    """
    POST /epin-vendor-api/transaction-status

    Fallback for rows with no callback received.
    Only call after minimum 45 seconds (scheduler enforces 2-minute minimum).

    Request  : JSON body encrypted with 3DES-ECB → sent as raw text
    Response : likely 3DES-ECB encrypted (same pattern as recharge)
               decrypt attempted first; plain JSON fallback if it fails.
    """
    body = {
        "transactionId": str(pyro_trans_id),
        "clientTxnId":   str(client_txn_id),
    }
    encrypted_body = encrypt(json.dumps(body), settings.pyro_secret_key)

    url = f"{settings.pyro_base_url}/epin-vendor-api/transaction-status"
    headers = {
        "apiKey":      settings.pyro_api_key,
        "accessToken": await token_manager.get_access_token(),
    }

    try:
        async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
            resp = await client.post(url, headers=headers, content=encrypted_body)

        data = _parse_pyro_response(resp, f"status-check clientTxnId={client_txn_id}")

        logger.info("Status check clientTxnId=%s pyroTxnId=%s: statusCode=%s",
                    client_txn_id, pyro_trans_id, data.get("statusCode"))
        return data

    except Exception as exc:
        logger.error("Status check error for clientTxnId=%s: %s", client_txn_id, exc)
        return {"statusCode": -1, "status": "ERROR", "message": str(exc)}
