
import base64
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings
from app.encryption import encrypt, decrypt

logger = logging.getLogger(__name__)


class PyroAuthService:

    def __init__(self):
        self.session_token:     Optional[str]   = None
        self.access_token:      Optional[str]   = None
        self._access_token_exp: Optional[float] = None  # Unix timestamp from JWT

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _base_headers(self) -> dict:
        return {"apiKey": settings.pyro_api_key}

    def _parse_jwt_exp(self, token: str) -> Optional[float]:
        """Extract exp claim from JWT payload — no signature validation needed."""
        try:
            part = token.split(".")[1]
            part += "=" * (-len(part) % 4)
            return float(json.loads(base64.b64decode(part)).get("exp", 0))
        except Exception:
            return None

    def _is_access_token_valid(self) -> bool:
        if not self.access_token or not self._access_token_exp:
            return False
        return self._access_token_exp > datetime.now(timezone.utc).timestamp() + 60

    def _parse_pyro_response(self, resp: httpx.Response, label: str) -> dict:
        """
        Parse a Pyro response as plain JSON first, with encrypted fallback.
        """
        raw = resp.text.strip()
        try:
            return resp.json()
        except Exception as json_err:
            logger.debug("%s: plain JSON parse failed (%s); trying encrypted response", label, json_err)

        try:
            return json.loads(decrypt(raw, settings.pyro_secret_key))
        except Exception as dec_err:
            logger.error("%s: both plain JSON and decrypt parse failed. Raw: %s", label, raw[:200])
            return {"statusCode": -1, "message": f"Response parse failed: {dec_err}"}

    # ── Public API ─────────────────────────────────────────────────────────────

    async def authenticate(self) -> bool:
        """
        POST /auth-api/authentication
        Request encrypted. Response is plain JSON in the current Pyro environment.
        Called on startup and daily by scheduler.
        """
        body = {"loginId": settings.pyro_login_id, "password": settings.pyro_password}
        encrypted_body = encrypt(json.dumps(body), settings.pyro_secret_key)

        async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
            resp = await client.post(
                f"{settings.pyro_base_url}/auth-api/authentication",
                headers={**self._base_headers(), "Content-Type": "application/json"},
                content=encrypted_body,
            )

        data = self._parse_pyro_response(resp, "AUTH")

        if data.get("statusCode") == 2000:
            d = data["data"]
            self.session_token     = d["sessionToken"]
            self.access_token      = d["accessToken"]
            self._access_token_exp = self._parse_jwt_exp(self.access_token)
            logger.info("Pyro authentication successful — user: %s", d.get("userName"))
            return True

        logger.error("Pyro authentication failed: %s — %s",
                     data.get("statusCode"), data.get("message"))
        return False

    async def refresh_access_token(self) -> bool:
        """
        GET /auth-api/refresh-access-token
        No request body. Response is plain JSON.
        Always called before generating an action token.
        """
        async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
            resp = await client.get(
                f"{settings.pyro_base_url}/auth-api/refresh-access-token",
                headers={
                    **self._base_headers(),
                    "sessionToken": self.session_token,
                    "accessToken":  self.access_token,
                },
            )
            data = resp.json()

        if data.get("statusCode") == 2000:
            self.access_token      = data["data"]["accessToken"]
            self._access_token_exp = self._parse_jwt_exp(self.access_token)
            logger.debug("Access token refreshed")
            return True

        logger.error("Token refresh failed: %s — %s",
                     data.get("statusCode"), data.get("message"))
        return False

    async def get_action_token(self) -> Optional[str]:
        """
        GET /auth-api/generate-action-token
        No request body. Response is plain JSON.
        Always refreshes access token first (Pyro requirement).
        actionToken: single-use, 1-minute expiry — generate immediately before recharge POST.
        """
        if not await self.refresh_access_token():
            logger.error("Cannot get action token — access token refresh failed")
            return None

        async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
            resp = await client.get(
                f"{settings.pyro_base_url}/auth-api/generate-action-token",
                headers={
                    **self._base_headers(),
                    "sessionToken": self.session_token,
                    "accessToken":  self.access_token,
                },
            )
            data = resp.json()

        if data.get("statusCode") == 2000:
            d = data["data"]
            self.access_token      = d["accessToken"]
            self._access_token_exp = self._parse_jwt_exp(self.access_token)
            logger.debug("Action token generated")
            return d["actionToken"]

        logger.error("Action token failed: %s — %s",
                     data.get("statusCode"), data.get("message"))
        return None

    async def get_access_token(self) -> Optional[str]:
        """Returns a valid access token, refreshing if needed."""
        if not self._is_access_token_valid():
            await self.refresh_access_token()
        return self.access_token


token_manager = PyroAuthService()
