"""Official Python SDK for the Akilent email API.

    from akilent import Akilent

    client = Akilent(api_key="ak_live_…")
    client.messages.send(from_="billing@acme.com", to="user@example.com",
                         subject="Receipt", text="Thanks!")
"""
from __future__ import annotations

from typing import Optional

import httpx

from ._transport import Transport, _DEFAULT_BASE_URL
from . import webhooks
from .errors import (
    AkilentError,
    APIConnectionError,
    APIStatusError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionError_,
    RateLimitError,
    ServerError,
    ValidationError,
)
from .resources import Campaigns, Messages, RequestLogs, Templates

__version__ = "0.1.0"
__all__ = [
    "Akilent",
    "webhooks",
    "AkilentError",
    "APIConnectionError",
    "APIStatusError",
    "AuthenticationError",
    "ConflictError",
    "NotFoundError",
    "PermissionError_",
    "RateLimitError",
    "ServerError",
    "ValidationError",
]


class Akilent:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 2,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._transport = Transport(
            api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            client=http_client,
        )
        self.messages = Messages(self._transport)
        self.templates = Templates(self._transport)
        self.campaigns = Campaigns(self._transport)
        self.request_logs = RequestLogs(self._transport)

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "Akilent":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
