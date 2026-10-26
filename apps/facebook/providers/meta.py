"""Messenger Platform + Page comments provider (Facebook Graph API, Page token).

All Meta-specific details live here: endpoints, the HUMAN_AGENT message tag and
which errors are terminal. Callers get a ``SendResult`` back and apply the state
change themselves (services/outbound.py), as on Instagram.

Permissions: pages_messaging (messages, private replies, typing), Business Asset
User Profile Access (names), pages_manage_engagement (reply to, hide and delete
comments, comment on posts), pages_read_user_content (read comments).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.conf import settings

from apps.instagram.providers.base import SendResult

logger = logging.getLogger(__name__)

GRAPH_API_BASE = f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_VERSION}"


@dataclass
class FacebookSendResult(SendResult):
    # The customer's Messenger PSID, which Meta returns for a send. For a private reply
    # it can differ from the id the comment webhook named the commenter by.
    recipient_id: str = ""


class FacebookAPIError(Exception):
    # Meta error codes that mean we must not retry
    _TERMINAL_CODES = {10, 100, 190, 200, 230, 368, 551}

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


class MetaFacebookProvider:
    """Sends on behalf of one connected Facebook Page."""

    def __init__(self, access_token: str, page_id: str):
        self._token = access_token
        self._page_id = page_id

    # -- transport ---------------------------------------------------------

    def _request(self, method: str, endpoint: str, *, json=None, params=None) -> dict:
        resp = requests.request(
            method,
            f"{GRAPH_API_BASE}/{endpoint}",
            json=json,
            params={**(params or {}), "access_token": self._token},
            timeout=10,
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not resp.ok or (isinstance(data, dict) and "error" in data):
            error = data.get("error", {}) if isinstance(data, dict) else {}
            raise FacebookAPIError(
                code=error.get("code", resp.status_code),
                message=error.get("message", resp.text[:200]),
                http_status=resp.status_code,
                subcode=error.get("error_subcode"),
                fbtrace_id=error.get("fbtrace_id", ""),
            )
        return data if isinstance(data, dict) else {}

    def _send(
        self, what: str, endpoint: str, payload: dict, id_key: str
    ) -> FacebookSendResult:
        try:
            data = self._request("post", endpoint, json=payload)
            return FacebookSendResult(
                success=True,
                provider_message_id=data.get(id_key, ""),
                recipient_id=str(data.get("recipient_id") or ""),
            )
        except FacebookAPIError as exc:
            logger.warning(
                "Facebook %s failed: code=%s subcode=%s msg=%s page=%s fbtrace_id=%s",
                what,
                exc.code,
                exc.subcode,
                exc.message,
                self._page_id,
                exc.fbtrace_id,
            )
            return FacebookSendResult(
                success=False,
                error=str(exc),
                terminal=exc.is_terminal,
                error_code=exc.code,
                http_status=exc.http_status,
            )
        except Exception as exc:
            logger.exception("Facebook %s unexpected error", what)
            return FacebookSendResult(success=False, error=str(exc), terminal=False)

    @staticmethod
    def _messaging(human_agent: bool) -> dict:
        """Inside 24 hours a reply is a RESPONSE; after that only a person's reply may go,
        tagged HUMAN_AGENT (up to 7 days after the customer's last message)."""
        if human_agent:
            return {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}
        return {"messaging_type": "RESPONSE"}

    # -- Messenger ---------------------------------------------------------

    def send_message(
        self,
        psid: str,
        body: str,
        *,
        human_agent: bool = False,
        quick_replies: list | None = None,
    ) -> SendResult:
        message: dict = {"text": body}
        if quick_replies:
            message["quick_replies"] = quick_replies
        payload = {
            "recipient": {"id": psid},
            "message": message,
            **self._messaging(human_agent),
        }
        return self._send(
            "send_message", f"{self._page_id}/messages", payload, "message_id"
        )

    def send_attachment(
        self, psid: str, kind: str, url: str, *, human_agent: bool = False
    ) -> SendResult:
        """Send an image / video / audio / file that Meta fetches from ``url``."""
        payload = {
            "recipient": {"id": psid},
            "message": {"attachment": {"type": kind, "payload": {"url": url}}},
            **self._messaging(human_agent),
        }
        return self._send(
            "send_attachment", f"{self._page_id}/messages", payload, "message_id"
        )

    def send_typing(self, psid: str) -> bool:
        """``typing_on``: cosmetic, so any failure is logged quietly and dropped."""
        try:
            self._request(
                "post",
                f"{self._page_id}/messages",
                json={"recipient": {"id": psid}, "sender_action": "typing_on"},
            )
            return True
        except Exception as exc:
            logger.debug("Facebook typing indicator not sent: %s", exc)
            return False

    def get_user_profile(self, psid: str) -> dict:
        """Name and picture of someone who messaged the Page (Business Asset User
        Profile Access). Empty when Meta won't say."""
        try:
            return self._request(
                "get", psid, params={"fields": "first_name,last_name,name,profile_pic"}
            )
        except Exception as exc:
            logger.debug("Facebook get_user_profile failed for %s: %s", psid, exc)
            return {}

    # -- comments ----------------------------------------------------------

    def private_reply(self, comment_id: str, body: str) -> SendResult:
        """A Messenger message in answer to a comment (7 days from the comment)."""
        payload = {
            "recipient": {"comment_id": comment_id},
            "message": {"text": body},
            "messaging_type": "RESPONSE",
        }
        return self._send(
            "private_reply", f"{self._page_id}/messages", payload, "message_id"
        )

    def reply_to_comment(self, comment_id: str, body: str) -> SendResult:
        """A public reply under a comment, as the Page."""
        return self._send(
            "reply_to_comment", f"{comment_id}/comments", {"message": body}, "id"
        )

    def comment_on_post(self, post_id: str, body: str) -> SendResult:
        """A new top-level comment on one of the Page's posts, as the Page."""
        return self._send(
            "comment_on_post", f"{post_id}/comments", {"message": body}, "id"
        )

    def hide_comment(self, comment_id: str, hidden: bool = True) -> tuple[bool, str]:
        """Hide (or unhide) a comment. Reversible."""
        return self._comment_call(
            "post", comment_id, {"is_hidden": "true" if hidden else "false"}
        )

    def delete_comment(self, comment_id: str) -> tuple[bool, str]:
        return self._comment_call("delete", comment_id, {})

    def get_post(self, post_id: str) -> dict:
        """A post's permalink, text and picture, for the Comments view. Best effort."""
        try:
            return self._request(
                "get", post_id, params={"fields": "permalink_url,message,full_picture"}
            )
        except Exception as exc:
            logger.debug("Facebook get_post failed for %s: %s", post_id, exc)
            return {}

    def _comment_call(
        self, method: str, comment_id: str, params: dict
    ) -> tuple[bool, str]:
        try:
            self._request(method, comment_id, params=params)
            return True, ""
        except FacebookAPIError as exc:
            logger.warning(
                "Facebook %s comment %s failed: code=%s subcode=%s msg=%s fbtrace_id=%s",
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
                "Facebook %s comment %s unexpected error", method, comment_id
            )
            return False, f"request failed: {exc}"

    # -- webhooks ----------------------------------------------------------

    def subscribe_page(self, fields: str) -> tuple[bool, str]:
        """Have the Page send this app its webhooks (pages_manage_metadata)."""
        try:
            data = self._request(
                "post",
                f"{self._page_id}/subscribed_apps",
                params={"subscribed_fields": fields},
            )
            return bool(data.get("success", True)), ""
        except FacebookAPIError as exc:
            logger.warning(
                "Facebook subscribe_page %s failed: code=%s msg=%s",
                self._page_id,
                exc.code,
                exc.message,
            )
            return False, str(exc)
        except Exception as exc:
            logger.exception("Facebook subscribe_page %s errored", self._page_id)
            return False, str(exc)

    def unsubscribe_page(self) -> bool:
        try:
            self._request("delete", f"{self._page_id}/subscribed_apps")
            return True
        except Exception as exc:
            logger.info("Facebook unsubscribe_page %s failed: %s", self._page_id, exc)
            return False


def provider_for(conn) -> MetaFacebookProvider:
    """The provider for a ``publishing.FacebookPageConnection``."""
    return MetaFacebookProvider(access_token=conn.access_token, page_id=conn.page_id)
