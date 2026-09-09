from __future__ import annotations

import time
import uuid
from typing import Any, Mapping, Optional

import httpx

from .errors import APIConnectionError, error_from_response

_DEFAULT_BASE_URL = "https://api.akilent.com"
_USER_AGENT = "akilent-python/0.1.0"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class Transport:
    """Thin wrapper over httpx with auth, retries, and idempotency helpers."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
        client: Optional[httpx.Client] = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        }
        if method.upper() == "POST" and idempotency_key is None:
            idempotency_key = str(uuid.uuid4())
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        attempt = 0
        while True:
            try:
                resp = self._client.request(
                    method, url, params=params, json=json, headers=headers
                )
            except httpx.HTTPError as exc:  # DNS / TLS / timeout / reset
                if attempt < self._max_retries:
                    attempt += 1
                    time.sleep(_backoff(attempt))
                    continue
                raise APIConnectionError(str(exc)) from exc

            if resp.status_code in _RETRY_STATUSES and attempt < self._max_retries:
                attempt += 1
                time.sleep(_retry_delay(resp, attempt))
                continue

            return _parse(resp)


def _parse(resp: httpx.Response) -> Any:
    body: Any
    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = None
    if resp.status_code >= 400:
        raise error_from_response(resp.status_code, body, resp.headers)
    return body


def _backoff(attempt: int) -> float:
    return min(0.5 * (2 ** (attempt - 1)), 8.0)


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    ra = resp.headers.get("retry-after")
    if ra and str(ra).isdigit():
        return float(ra)
    return _backoff(attempt)
