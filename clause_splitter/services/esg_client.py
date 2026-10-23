"""ESG open-platform gateway client — token flow + fixed retries.

Parity with the previous-version service (esg_callback.py): client_credentials
token, ``ret=13012`` → refresh token and retry once, ``ret=13016`` → sleep and
retry up to ``max_retries``. Sync ``requests`` on purpose; callers wrap in
``asyncio.to_thread``.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import requests

from clause_splitter.utils import get_logger

logger = get_logger()

#: Gateway return codes (previous-version contract).
RETRY_TOKEN_EXPIRED = "13012"  # refresh token, retry once
RETRY_TIMEOUT = "13016"  # sleep and retry

_RETRY_DELAY_S = 60.0


class EsgClient:
    """Posts result callbacks through the ESG open platform."""

    def __init__(
        self,
        *,
        auth_end_point: str,
        open_end_point: str,
        client_id: str,
        client_secret: str,
        callback_path: str,
        timeout_s: float = 30.0,
        max_retries: int = 5,
    ) -> None:
        self.auth_end_point = auth_end_point
        self.open_end_point = open_end_point.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_path = callback_path
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._token: str | None = None

    # ── token ─────────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        url = (
            f"{self.auth_end_point}?client_id={self.client_id}"
            f"&grant_type=client_credentials&client_secret={self.client_secret}"
        )
        result = requests.get(url, timeout=self.timeout_s).json()
        if str(result.get("ret")) != "0":
            raise RuntimeError(f"esg auth failed: {result}")
        return result["data"]["access_token"]

    def _token_or_refresh(self) -> str:
        if self._token is None:
            self._token = self._get_token()
        return self._token

    # ── callback ──────────────────────────────────────────────────────────

    def post_callback(self, payload: dict[str, Any], _retries: int | None = None) -> dict[str, Any]:
        """POST the result payload; token-expired / timeout codes retry."""
        retries = self.max_retries if _retries is None else _retries
        url = (
            f"{self.open_end_point}{self.callback_path}"
            f"?access_token={self._token_or_refresh()}&request_id={uuid.uuid1()}"
        )
        try:
            result = requests.post(
                url, json=payload, headers={"Content-Type": "application/json"},
                timeout=self.timeout_s,
            ).json()
        except Exception:
            if retries > 0:
                time.sleep(_RETRY_DELAY_S)
                return self.post_callback(payload, _retries=retries - 1)
            raise

        code = str(result.get("ret", ""))
        if code == RETRY_TOKEN_EXPIRED:
            self._token = self._get_token()
            return self.post_callback(payload, _retries=retries)
        if code == RETRY_TIMEOUT and retries > 0:
            time.sleep(_RETRY_DELAY_S)
            return self.post_callback(payload, _retries=retries - 1)
        return result


def get_esg_client() -> EsgClient | None:
    """Configured client, or None when the gateway is not set up (dev fallback)."""
    from clause_splitter import config

    if not config.ESG_OPEN_END_POINT:
        return None
    return EsgClient(
        auth_end_point=config.ESG_AUTH_END_POINT,
        open_end_point=config.ESG_OPEN_END_POINT,
        client_id=config.ESG_CLIENT_ID,
        client_secret=config.ESG_CLIENT_SECRET,
        callback_path=config.ESG_CALLBACK_PATH,
    )
