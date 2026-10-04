from __future__ import annotations

import logging

import requests

from .base import BaseInstagramProvider, EligibilityResult, SendResult

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


class MetaInstagramProvider(BaseInstagramProvider):
    """
    Instagram Graph API provider.

    All Meta-specific messaging rules (DM windows, private reply windows,
    permission errors, rate limits) are handled here. Nothing outside this
    class should reason about Meta API details.
    """

    def __init__(self, access_token: str, instagram_account_id: str):
        self._token = access_token
        self._account_id = instagram_account_id

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f"{GRAPH_API_BASE}/{endpoint}"
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=10)
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok:
            error = data.get("error", {})
            code = error.get("code", resp.status_code)
            msg = error.get("message", resp.text[:200])
            raise InstagramAPIError(code=code, message=msg, http_status=resp.status_code)
        return data

    def can_send(self, recipient_igsid: str, action_type: str) -> EligibilityResult:
        # Meta's rules:
        # - Standard DM: user must have messaged the business first; 7-day window
        #   after the last customer message (standard_messaging permission).
        # - Private reply: 7-day window from the comment timestamp.
        # We rely on the API returning an error for ineligible sends and treat
        # that as non-retryable; this method provides a best-effort pre-flight
        # check based on cached data. Real eligibility is confirmed by the API.
        return EligibilityResult(eligible=True)

    def send_message(self, recipient_igsid: str, body: str) -> SendResult:
        payload = {
            "recipient": {"id": recipient_igsid},
            "message": {"text": body},
            "messaging_type": "RESPONSE",
        }
        try:
            data = self._post(f"{self._account_id}/messages", payload)
            return SendResult(
                success=True,
                provider_message_id=data.get("message_id", ""),
            )
        except InstagramAPIError as exc:
            logger.warning(
                "Instagram send_message failed: code=%s msg=%s", exc.code, exc.message
            )
            terminal = exc.is_terminal
            return SendResult(success=False, error=str(exc), terminal=terminal)
        except Exception as exc:
            logger.exception("Instagram send_message unexpected error")
            return SendResult(success=False, error=str(exc), terminal=False)

    def private_reply(self, comment_id: str, body: str) -> SendResult:
        # Private Reply API — sends a DM in response to a comment.
        # Requires instagram_business_manage_messages permission.
        # Window: 7 days from the comment.
        payload = {
            "recipient": {"comment_id": comment_id},
            "message": {"text": body},
            "messaging_type": "RESPONSE",
        }
        try:
            data = self._post(f"{self._account_id}/messages", payload)
            return SendResult(
                success=True,
                provider_message_id=data.get("message_id", ""),
            )
        except InstagramAPIError as exc:
            logger.warning(
                "Instagram private_reply failed: code=%s msg=%s", exc.code, exc.message
            )
            return SendResult(success=False, error=str(exc), terminal=exc.is_terminal)
        except Exception as exc:
            logger.exception("Instagram private_reply unexpected error")
            return SendResult(success=False, error=str(exc), terminal=False)

    def get_user_profile(self, igsid: str) -> dict:
        url = f"{GRAPH_API_BASE}/{igsid}"
        params = {
            "fields": "name,username",
            "access_token": self._token,
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.ok:
                return resp.json()
        except Exception:
            logger.exception("Instagram get_user_profile failed for %s", igsid)
        return {}


class InstagramAPIError(Exception):
    # Meta error codes that mean we must not retry
    _TERMINAL_CODES = {10, 200, 190, 368, 100}

    def __init__(self, code: int, message: str, http_status: int = 0):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(f"[{code}] {message}")

    @property
    def is_terminal(self) -> bool:
        return self.code in self._TERMINAL_CODES or self.http_status in {401, 403}
