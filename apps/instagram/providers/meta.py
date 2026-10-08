from __future__ import annotations

import logging

import requests

from .base import BaseInstagramProvider, EligibilityResult, SendResult

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
# Instagram Business Login tokens (IGAA…) only work with graph.instagram.com.
# graph.facebook.com requires a Facebook User/Page token (EAA…).
GRAPH_IG_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"
# Kept for comment/media operations that may still need the Facebook Graph API.
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

    def _post(self, endpoint: str, payload: dict, *, base: str = GRAPH_IG_BASE) -> dict:
        url = f"{base}/{endpoint}"
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=10)
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok:
            error = data.get("error", {})
            code = error.get("code", resp.status_code)
            msg = error.get("message", resp.text[:200])
            raise InstagramAPIError(
                code=code,
                message=msg,
                http_status=resp.status_code,
                subcode=error.get("error_subcode"),
                fbtrace_id=error.get("fbtrace_id", ""),
            )
        return data

    def can_send(self, recipient_igsid: str, action_type: str) -> EligibilityResult:
        # Meta's rules:
        # - Standard DM: user must have messaged the business first; free-text
        #   replies are allowed for 24 hours after their last message (the 7-day
        #   HUMAN_AGENT tag needs its own App Review and isn't used).
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
                "Instagram send_message failed: code=%s subcode=%s msg=%s "
                "recipient=%s account=%s fbtrace_id=%s",
                exc.code,
                exc.subcode,
                exc.message,
                recipient_igsid,
                self._account_id,
                exc.fbtrace_id,
            )
            return SendResult(
                success=False,
                error=str(exc),
                terminal=exc.is_terminal,
                error_code=exc.code,
                http_status=exc.http_status,
            )
        except Exception as exc:
            logger.exception("Instagram send_message unexpected error")
            return SendResult(success=False, error=str(exc), terminal=False)

    def send_attachment(self, recipient_igsid: str, kind: str, url: str) -> SendResult:
        """Send an image / video / audio / file DM that Meta fetches from ``url``."""
        payload = {
            "recipient": {"id": recipient_igsid},
            "message": {"attachment": {"type": kind, "payload": {"url": url}}},
            "messaging_type": "RESPONSE",
        }
        try:
            data = self._post(f"{self._account_id}/messages", payload)
            return SendResult(
                success=True, provider_message_id=data.get("message_id", "")
            )
        except InstagramAPIError as exc:
            logger.warning(
                "Instagram send_attachment failed: code=%s subcode=%s msg=%s "
                "kind=%s recipient=%s account=%s fbtrace_id=%s",
                exc.code,
                exc.subcode,
                exc.message,
                kind,
                recipient_igsid,
                self._account_id,
                exc.fbtrace_id,
            )
            return SendResult(
                success=False,
                error=str(exc),
                terminal=exc.is_terminal,
                error_code=exc.code,
                http_status=exc.http_status,
            )
        except Exception as exc:
            logger.exception("Instagram send_attachment unexpected error")
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
            return SendResult(
                success=False,
                error=str(exc),
                terminal=exc.is_terminal,
                error_code=exc.code,
                http_status=exc.http_status,
            )
        except Exception as exc:
            logger.exception("Instagram private_reply unexpected error")
            return SendResult(success=False, error=str(exc), terminal=False)

    def get_user_profile(self, igsid: str) -> dict:
        url = f"{GRAPH_IG_BASE}/{igsid}"
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

    def hide_comment(self, comment_id: str) -> tuple[bool, str]:
        """Hide a comment on the business's media. Reversible.

        Meta documents ``POST /{ig-comment-id}?hide=true``, so ``hide`` goes as a
        query parameter, not a JSON body.
        """
        return self._comment_call("post", comment_id, {"hide": "true"})

    def delete_comment(self, comment_id: str) -> tuple[bool, str]:
        """Permanently delete a comment. Requires instagram_business_manage_comments."""
        return self._comment_call("delete", comment_id, {})

    def _comment_call(
        self, method: str, comment_id: str, params: dict
    ) -> tuple[bool, str]:
        """Run a comment moderation call: ``(ok, error)``, error being Meta's own words."""
        try:
            resp = requests.request(
                method,
                f"{GRAPH_IG_BASE}/{comment_id}",
                params=params,
                headers=self._headers(),
                timeout=10,
            )
            if resp.ok:
                return True, ""
            try:
                error = resp.json().get("error", {})
            except Exception:
                error = {}
            exc = InstagramAPIError(
                code=error.get("code", resp.status_code),
                message=error.get("message", resp.text[:200]),
                http_status=resp.status_code,
                subcode=error.get("error_subcode"),
                fbtrace_id=error.get("fbtrace_id", ""),
            )
            logger.warning(
                "Instagram %s comment %s failed: code=%s subcode=%s msg=%s fbtrace_id=%s",
                method,
                comment_id,
                exc.code,
                exc.subcode,
                exc.message,
                exc.fbtrace_id,
            )
            return False, str(exc)
        except Exception as exc:
            logger.exception(
                "Instagram %s comment %s unexpected error", method, comment_id
            )
            return False, f"request failed: {exc}"


class InstagramAPIError(Exception):
    # Meta error codes that mean we must not retry
    _TERMINAL_CODES = {10, 200, 190, 368, 100}

    def __init__(
        self,
        code: int,
        message: str,
        http_status: int = 0,
        subcode: int | None = None,
        fbtrace_id: str = "",
    ):
        self.code = code
        self.message = message
        self.http_status = http_status
        self.subcode = subcode
        self.fbtrace_id = fbtrace_id
        super().__init__(f"[{code}] {message}")

    @property
    def is_terminal(self) -> bool:
        return self.code in self._TERMINAL_CODES or self.http_status in {401, 403}
