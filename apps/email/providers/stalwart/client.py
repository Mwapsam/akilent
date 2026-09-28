"""Stalwart JMAP Management API client.

Stalwart manages domains and accounts as Stalwart-proprietary JMAP objects
(``x:Domain``, ``x:Account``, ``x:MailingList``, ...) under capability
``urn:stalwart:jmap`` — confirmed live against the server's own schema
introspection and round-tripped x:Domain/get + x:Domain/set calls. The
generic IETF ``Principal`` object/capability (``urn:ietf:params:jmap:principals``)
also exists on this server but represents sharing principals (user/group/
resource/location), not domains — it is NOT used here. Everything goes
through a single ``POST /jmap`` endpoint carrying a batch of
``[method, arguments, callId]`` triples.

Responsibilities:
  - Auth via a static, pre-generated Bearer API key (STALWART_API_KEY)
  - Building/sending one-call JMAP request batches
  - Retry with exponential back-off on transient network/5xx errors
  - Timeout handling (connect=5s, read=15s)
  - HTTP status code / JMAP method-level error → typed domain exception mapping
  - Structured per-request DEBUG logging

No business logic lives here — only the mechanics of talking to the API.
Instantiate StalwartApiClient.from_settings() to get a ready-to-use client.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from apps.email.exceptions import (
    AuthenticationError,
    ConfigurationError,
    EmailProviderError,
    ProviderTimeoutError,
    RateLimitError,
    ResourceConflictError,
    ResourceNotFoundError,
    ValidationError,
)

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 5  # seconds to establish TCP connection
_READ_TIMEOUT = 15  # seconds to receive server response

_CAPABILITIES = [
    "urn:ietf:params:jmap:core",
    "urn:stalwart:jmap",
]

# JMAP SetError/method-error "type" → exception class
_ERROR_TYPE_MAP: dict[str, type[EmailProviderError]] = {
    "notFound": ResourceNotFoundError,
    "alreadyExists": ResourceConflictError,
    "forbidden": AuthenticationError,
    "unauthorized": AuthenticationError,
    "invalidProperties": ValidationError,
    "invalidArguments": ValidationError,
}


def _build_session() -> requests.Session:
    """Build a requests.Session with retry adapter and connection pooling."""
    retry = Retry(
        total=3,
        backoff_factor=0.4,  # delays: 0.4s, 0.8s, 1.6s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,  # we map status codes to exceptions below
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=8,
    )
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class StalwartApiClient:
    """Low-level JMAP client for the Stalwart Principal management API.

    Usage::

        client = StalwartApiClient.from_settings()
        created = client.call("Principal/set", {"create": {"d1": {...}}})
        ids = client.call("Principal/query", {"filter": {"type": "domain"}})
    """

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._session = _build_session()

    @classmethod
    def from_settings(cls) -> StalwartApiClient:
        """Construct from Django settings / environment variables.

        Stalwart authenticates Management API requests with a static,
        pre-generated Bearer API key (format ``API_...``), not a
        username/password login exchange.
        """
        base_url = getattr(settings, "STALWART_API_BASE", "") or ""
        api_key = getattr(settings, "STALWART_API_KEY", "") or ""
        if not base_url or not api_key:
            raise ConfigurationError(
                "STALWART_API_BASE and STALWART_API_KEY must both be set."
            )
        return cls(base_url.rstrip("/"), api_key)

    # ── JMAP method calls ────────────────────────────────────────────────

    def call(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Send a single JMAP method call and return its response arguments.

        Raises a typed EmailProviderError if the transport fails, the HTTP
        status is non-2xx, or Stalwart returns a JMAP-level "error" response.
        """
        url = f"{self._base}/jmap"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = {
            "using": _CAPABILITIES,
            "methodCalls": [[method, arguments, "c1"]],
        }
        t0 = time.monotonic()
        try:
            resp = self._session.post(
                url,
                json=body,
                headers=headers,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
        except requests.Timeout as exc:
            raise ProviderTimeoutError(method) from exc
        except requests.ConnectionError as exc:
            raise EmailProviderError(
                f"Connection to Stalwart failed: {exc}",
                code="connection_error",
            ) from exc

        elapsed = time.monotonic() - t0
        logger.debug(
            "stalwart jmap %s status=%s elapsed=%.2fs",
            method,
            resp.status_code,
            elapsed,
        )

        self._raise_for_status(resp, method)
        return self._extract_response(resp, method)

    def _extract_response(self, resp: requests.Response, method: str) -> dict[str, Any]:
        data = self._parse(resp)
        responses = data.get("methodResponses") or []
        if not responses:
            raise EmailProviderError(
                f"Stalwart returned no methodResponses for {method}.",
                code="empty_response",
            )
        name, args, _call_id = responses[0]
        if name == "error":
            self._raise_jmap_error(method, args)
        return args

    @staticmethod
    def _raise_jmap_error(method: str, args: dict[str, Any]) -> None:
        err_type = args.get("type", "unknown")
        description = args.get("description") or str(args)
        exc_cls = _ERROR_TYPE_MAP.get(err_type, EmailProviderError)
        if exc_cls is ResourceNotFoundError:
            raise ResourceNotFoundError(method, description)
        if exc_cls is ResourceConflictError:
            raise ResourceConflictError(method, description)
        if exc_cls is AuthenticationError:
            raise AuthenticationError(f"Stalwart {method} rejected: {description}")
        if exc_cls is ValidationError:
            raise ValidationError(f"Stalwart {method} rejected input: {description}")
        raise EmailProviderError(
            f"Stalwart {method} → {err_type}: {description}",
            code=f"jmap_{err_type}",
        )

    # ── Status → exception mapping ────────────────────────────────────────

    @staticmethod
    def _extract_error(resp: requests.Response) -> str:
        try:
            body = resp.json()
            return (
                body.get("detail")
                or body.get("error")
                or body.get("message")
                or resp.text[:300]
            )
        except ValueError:
            return resp.text[:300]

    def _raise_for_status(self, resp: requests.Response, method: str) -> None:
        if resp.ok:
            return
        error = self._extract_error(resp)
        status = resp.status_code

        if status == 401:
            raise AuthenticationError(f"Stalwart rejected credentials: {error}")
        if status == 404:
            raise ResourceNotFoundError("jmap", method)
        if status == 409:
            raise ResourceConflictError("jmap", method)
        if status == 422:
            raise ValidationError(f"Stalwart rejected input for {method}: {error}")
        if status == 429:
            retry_after = resp.headers.get("Retry-After")
            raise RateLimitError(int(retry_after) if retry_after else None)
        raise EmailProviderError(
            f"Stalwart {method} → HTTP {status}: {error}",
            code=f"http_{status}",
        )

    @staticmethod
    def _parse(resp: requests.Response) -> dict[str, Any]:
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}
