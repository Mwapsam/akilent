"""PayPal REST client (Orders v2, Payments v2, webhook verification).

Shapes and error codes are pinned in docs/billing/paypal/CONTRACT.md, against recorded sandbox
responses in docs/billing/paypal/fixtures/sandbox/.

Safety rules:
- Every POST carries a ``PayPal-Request-Id``; sending it again returns PayPal's first answer.
- A POST is never retried here. If it times out or PayPal answers 5xx, nobody knows whether it
  took effect, so ``PayPalUncertain`` is raised and the caller looks the outcome up instead.
- GETs and the token call are retried a couple of times; they change nothing.
- Logs carry PayPal's debug id and issue code, never credentials, tokens or payer details.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}
TIMEOUT = (5, 30)  # connect, read
GET_ATTEMPTS = 3


class PayPalError(Exception):
    """PayPal refused the request. ``issue`` is its machine code, e.g. DUPLICATE_INVOICE_ID."""

    def __init__(
        self, message: str, *, status: int = 0, issue: str = "", debug_id: str = ""
    ):
        super().__init__(message)
        self.status = status
        self.issue = issue
        self.debug_id = debug_id


class PayPalUncertain(PayPalError):
    """A POST timed out or failed on PayPal's side: it may or may not have taken effect."""


def _error(response) -> PayPalError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    details = body.get("details") or [{}]
    issue = details[0].get("issue") or body.get("name") or ""
    debug_id = body.get("debug_id") or response.headers.get("PayPal-Debug-Id", "")
    return PayPalError(
        f"PayPal {response.status_code} {issue}".strip(),
        status=response.status_code,
        issue=issue,
        debug_id=debug_id,
    )


class PayPalClient:
    def __init__(self, client_id: str, client_secret: str, env: str = "sandbox"):
        if env not in BASE_URLS:
            raise PayPalError(f"PAYPAL_ENV must be sandbox or live, not {env!r}")
        self.base_url = BASE_URLS[env]
        self._client_id = client_id
        self._client_secret = client_secret
        self._cache_key = (
            f"paypal:token:{env}:{hashlib.sha256(client_id.encode()).hexdigest()[:12]}"
        )
        self._session = requests.Session()

    # --- auth ----------------------------------------------------------------------------------

    def _token(self, *, fresh: bool = False) -> str:
        if not fresh:
            token = cache.get(self._cache_key)
            if token:
                return token
        last: PayPalError | None = None
        for attempt in range(GET_ATTEMPTS):
            try:
                r = self._session.post(
                    f"{self.base_url}/v1/oauth2/token",
                    auth=(self._client_id, self._client_secret),
                    data={"grant_type": "client_credentials"},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                last = PayPalError(f"PayPal token request failed: {type(exc).__name__}")
            else:
                if r.status_code == 200:
                    data = r.json()
                    ttl = max(int(data.get("expires_in") or 0) - 60, 60)
                    cache.set(self._cache_key, data["access_token"], ttl)
                    return data["access_token"]
                last = _error(r)
                if r.status_code < 500:
                    break  # bad credentials won't get better
            time.sleep(0.5 * (attempt + 1))
        raise last or PayPalError("PayPal token request failed")

    # --- transport -----------------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json=None,
        data=None,
        params=None,
        request_id="",
    ):
        url = f"{self.base_url}{path}"
        is_get = method == "GET"
        attempts = GET_ATTEMPTS if is_get else 1
        refreshed = False
        attempt = 0
        while True:
            headers = {
                "Authorization": f"Bearer {self._token(fresh=refreshed)}",
                "Content-Type": "application/json",
            }
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            try:
                r = self._session.request(
                    method,
                    url,
                    headers=headers,
                    json=json,
                    data=data,
                    params=params,
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                attempt += 1
                if not is_get:
                    raise PayPalUncertain(
                        f"PayPal {method} {path} got no answer: {type(exc).__name__}"
                    ) from exc
                if attempt >= attempts:
                    raise PayPalError(f"PayPal {path} unreachable") from exc
                time.sleep(0.5 * attempt)
                continue
            if r.status_code == 401 and not refreshed:
                refreshed = True  # the cached token expired early; get a new one once
                continue
            if r.status_code >= 500:
                attempt += 1
                if not is_get:
                    err = _error(r)
                    raise PayPalUncertain(
                        str(err),
                        status=err.status,
                        issue=err.issue,
                        debug_id=err.debug_id,
                    )
                if attempt >= attempts:
                    raise _error(r)
                time.sleep(0.5 * attempt)
                continue
            if r.status_code >= 400:
                err = _error(r)
                logger.info(
                    "paypal %s %s: %s debug_id=%s",
                    method,
                    path,
                    err.issue,
                    err.debug_id,
                )
                raise err
            return r.json() if r.content else {}

    # --- API -----------------------------------------------------------------------------------

    def create_order(self, body: dict, *, request_id: str) -> dict:
        return self._request(
            "POST", "/v2/checkout/orders", json=body, request_id=request_id
        )

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{order_id}")

    def capture_order(self, order_id: str, *, request_id: str) -> dict:
        return self._request(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            json={},
            request_id=request_id,
        )

    def get_capture(self, capture_id: str) -> dict:
        return self._request("GET", f"/v2/payments/captures/{capture_id}")

    def get_refund(self, refund_id: str) -> dict:
        return self._request("GET", f"/v2/payments/refunds/{refund_id}")

    def verify_webhook_signature(self, fields: dict, raw_event: str) -> bool:
        """``fields`` are the transmission headers and webhook_id; ``raw_event`` is the body
        exactly as PayPal sent it. It is spliced in unchanged, because a re-serialised event
        (different escaping or spacing) may not verify. Read-only at PayPal."""
        head = json.dumps(fields)
        body = head[:-1] + ', "webhook_event": ' + raw_event + "}"
        data = self._request(
            "POST",
            "/v1/notifications/verify-webhook-signature",
            data=body.encode("utf-8"),
        )
        return data.get("verification_status") == "SUCCESS"


_clients: dict[tuple, PayPalClient] = {}


def config(name: str) -> str:
    """A PAYPAL_* setting. settings.py has already normalised it (stripped, env lower-cased)."""
    return getattr(settings, name, "") or ""


def get_paypal_client() -> PayPalClient:
    """One client per environment and credentials, so its connection pool is reused across the
    several calls a return or webhook makes. Changing the settings gives a new client."""
    client_id = config("PAYPAL_CLIENT_ID")
    secret = config("PAYPAL_CLIENT_SECRET")
    env = config("PAYPAL_ENV")
    if not client_id or not secret:
        raise PayPalError("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not configured")
    key = (env, client_id, hashlib.sha256(secret.encode()).hexdigest())
    client = _clients.get(key)
    if client is None:
        client = _clients[key] = PayPalClient(client_id, secret, env)
    return client
