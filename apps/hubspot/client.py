"""Thin HubSpot API client with automatic token refresh and retry.

Supports:
- OAuth token refresh via get_fresh_client()
- Rate-limit handling (429 → Retry-After)
- Simple GET/POST/PATCH wrappers
"""

from __future__ import annotations

import logging
import time

import requests
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
_MAX_RETRIES = 3


class HubSpotAPIError(Exception):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HubSpot API {status_code}: {body[:200]}")


class HubSpotClient:
    def __init__(self, access_token: str):
        self._token = access_token
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    def get(self, path: str, **params) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, json: dict) -> dict:
        return self._request("POST", path, json=json)

    def patch(self, path: str, json: dict) -> dict:
        return self._request("PATCH", path, json=json)

    def delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{_BASE}{path}"
        for attempt in range(_MAX_RETRIES):
            resp = self._session.request(method, url, **kwargs)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                logger.warning(
                    "HubSpot 429 - sleeping %ds (attempt %d)", retry_after, attempt
                )
                time.sleep(retry_after)
                continue
            if resp.status_code >= 400:
                raise HubSpotAPIError(resp.status_code, resp.text)
            if resp.status_code == 204 or not resp.content:
                return {}
            return resp.json()
        raise HubSpotAPIError(429, "Rate limit exhausted after retries")


def get_fresh_client(connection) -> HubSpotClient:
    """Return a client with a valid access token, refreshing if needed.

    Locks the connection row so concurrent refreshes don't race.
    Raises HubSpotAPIError on refresh failure.
    """
    from apps.hubspot.models import HubSpotConnection

    with transaction.atomic():
        conn = HubSpotConnection.objects.select_for_update().get(pk=connection.pk)
        if conn.status == HubSpotConnection.Status.DISCONNECTED:
            raise HubSpotAPIError(401, "Connection is disconnected")

        if conn.is_token_fresh():
            return HubSpotClient(conn.access_token or "")

        # Refresh the token.
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": _get_client_id(),
                "client_secret": _get_client_secret(),
                "refresh_token": conn.refresh_token,
            },
            timeout=30,
        )
        if resp.status_code >= 400:
            conn.status = HubSpotConnection.Status.PAUSED
            conn.save(update_fields=["status", "updated_at"])
            raise HubSpotAPIError(resp.status_code, resp.text)

        data = resp.json()
        from datetime import timedelta

        conn.access_token = data["access_token"]
        conn.refresh_token = data.get("refresh_token", conn.refresh_token)
        expires_in = data.get("expires_in", 1800)
        conn.token_expires_at = timezone.now() + timedelta(seconds=expires_in - 60)
        conn.save(
            update_fields=[
                "access_token",
                "refresh_token",
                "token_expires_at",
                "updated_at",
            ]
        )

    return HubSpotClient(conn.access_token)


def _get_client_id() -> str:
    from django.conf import settings

    return getattr(settings, "HUBSPOT_CLIENT_ID", "")


def _get_client_secret() -> str:
    from django.conf import settings

    return getattr(settings, "HUBSPOT_CLIENT_SECRET", "")
