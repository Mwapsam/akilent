"""Meta Cloud API WhatsApp provider implementation.

This is the production provider for sending WhatsApp messages via Meta's
official Cloud API. It handles:
  - Text, template, and media messages
  - Media URL retrieval and download
  - Read receipt marking

Errors are raised as WhatsAppProviderError; adapting is the provider's
responsibility, not the caller's.
"""
import logging

import requests
from django.conf import settings

from apps.whatsapp.providers.base import WhatsAppProvider, WhatsAppProviderError
from apps.whatsapp.types import (
    MediaHandleResult,
    MediaUploadResult,
    MediaUrlResult,
    ReadReceiptResult,
    SendResult,
)

logger = logging.getLogger(__name__)

_GRAPH_HOST = "https://graph.facebook.com"


def _graph_api_base() -> str:
    """Graph API base URL, versioned from a single setting (WHATSAPP_GRAPH_VERSION)."""
    version = getattr(settings, "WHATSAPP_GRAPH_VERSION", "v21.0")
    return f"{_GRAPH_HOST}/{version}"


# Meta Cloud API error codes that will not succeed on retry (permanent / policy).
# Everything else is treated as retryable (transient / rate limiting).
_NON_RETRYABLE_CODES = {
    "131047",  # re-engagement required (outside 24h window, no template)
    "131026",  # message undeliverable
    "131051",  # unsupported message type
    "131052",  # media download error (recipient)
    # 133010 = "phone number not registered". Treated as terminal: connect_complete
    # registers the number synchronously during Embedded Signup, so a send should
    # never race ahead of registration. If spurious 133010s ever appear right
    # after onboarding (WABA subscription / registration not yet propagated on
    # Meta's side), the failure-spike alerter surfaces the pattern within the
    # hour; revisit making *this* code retryable-with-short-cap if that happens.
    "133010",  # phone number not registered
    "132000", "132001", "132005", "132007", "132012", "132015", "132016", "132068", "132069",  # template errors
    "133004", "133005", "133006", "133008", "133009", "133016",  # account/registration errors
    "190",     # access token expired/invalid
}


def _classify_meta_error(status_code: int, body: dict) -> tuple[str, str, bool]:
    """Return (code, message, retryable) from a Meta error response body."""
    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
    code = str(err.get("code", "")) or str(status_code)
    subcode = err.get("error_subcode")
    if subcode:
        code = f"{code}/{subcode}"
    message = err.get("message") or err.get("error_data", {}).get("details") or "Meta API error"
    base = code.split("/")[0]
    retryable = not (base in _NON_RETRYABLE_CODES) and status_code not in (400, 401, 403)
    if status_code == 429 or base in ("130429", "131056", "80007"):
        retryable = True
    return code, message, retryable


class MetaCloudAPIProvider(WhatsAppProvider):
    """Meta Cloud API implementation of WhatsAppProvider.

    Sends messages via Meta's official WhatsApp Cloud API.
    """

    def __init__(self, access_token: str, phone_number_id: str, app_id: str = ""):
        """Initialize with Meta API credentials.

        Args:
            access_token: Meta API bearer token.
            phone_number_id: WhatsApp Business Phone Number ID.
            app_id: Meta app id — only needed for upload_template_media()'s
                app-scoped resumable upload, unused by every other method.
        """
        self.phone_number_id = phone_number_id
        self._access_token = access_token
        self._app_id = app_id
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    def _url(self, path: str) -> str:
        """Build a full Graph API URL from a path."""
        return f"{_graph_api_base()}/{path}"

    def _post_message(self, payload: dict) -> dict:
        """Post a message payload to the Cloud API.

        Returns the raw API response dict.

        Raises:
            WhatsAppProviderError: on network or API error. When Meta returned a
            structured error the exception carries ``.code`` and ``.retryable``.
        """
        url = self._url(f"{self.phone_number_id}/messages")
        try:
            response = self._session.post(url, json=payload, timeout=10)
        except requests.RequestException as e:
            err = WhatsAppProviderError(f"Failed to post message: {e}")
            err.code = ""
            err.retryable = True
            raise err from e

        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            code, message, retryable = _classify_meta_error(response.status_code, body)
            err = WhatsAppProviderError(f"Meta API {response.status_code} [{code}]: {message}")
            err.code = code
            err.retryable = retryable
            raise err
        return response.json()

    def _fail(self, to: str, kind: str, e: Exception) -> SendResult:
        logger.error("Failed to send %s to %s: %s", kind, to, e)
        return SendResult(
            message_id="",
            success=False,
            error=str(e),
            error_code=getattr(e, "code", None) or None,
            retryable=getattr(e, "retryable", True),
        )

    def send_text(self, to: str, body: str) -> SendResult:
        """Send a plain text message."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "text",
            "text": {"body": body},
        }
        try:
            result = self._post_message(payload)
            return SendResult(message_id=result["messages"][0]["id"], success=True)
        except (KeyError, IndexError, WhatsAppProviderError) as e:
            return self._fail(to, "text message", e)

    def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        components: list,
    ) -> SendResult:
        """Send a pre-approved template message."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                "components": components,
            },
        }
        try:
            result = self._post_message(payload)
            return SendResult(message_id=result["messages"][0]["id"], success=True)
        except (KeyError, IndexError, WhatsAppProviderError) as e:
            return self._fail(to, f"template '{template_name}'", e)

    def send_media(
        self,
        to: str,
        media_type: str,
        media_id: str,
        caption: str = "",
    ) -> SendResult:
        """Send a media message (image, video, document, audio)."""
        payload = {
            "messaging_product": "whatsapp",
            "to": to.lstrip("+"),
            "type": media_type,
            media_type: {"id": media_id, "caption": caption},
        }
        try:
            result = self._post_message(payload)
            return SendResult(message_id=result["messages"][0]["id"], success=True)
        except (KeyError, IndexError, WhatsAppProviderError) as e:
            return self._fail(to, media_type, e)

    def list_templates(self, waba_id: str) -> list:
        """Fetch all message templates for a WABA (follows paging)."""
        url = self._url(f"{waba_id}/message_templates")
        params = {"limit": 200}
        out: list = []
        try:
            while url:
                response = self._session.get(url, params=params, timeout=30)
                response.raise_for_status()
                body = response.json()
                out.extend(body.get("data", []))
                url = body.get("paging", {}).get("next")
                params = None  # `next` is a fully-formed URL
            return out
        except requests.RequestException as e:
            raise WhatsAppProviderError(
                f"Failed to list templates for {waba_id}: {e}"
            ) from e

    def create_template(self, waba_id: str, payload: dict) -> dict:
        """Submit a new message template for approval."""
        url = self._url(f"{waba_id}/message_templates")
        try:
            response = self._session.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            raise WhatsAppProviderError(f"Failed to create template: {e}") from e

        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            code, message, _ = _classify_meta_error(response.status_code, body)
            err = WhatsAppProviderError(f"Meta API {response.status_code} [{code}]: {message}")
            err.code = code
            raise err
        return response.json()

    def upload_media(
        self, content: bytes, mime_type: str, filename: str = "upload"
    ) -> MediaUploadResult:
        """Upload media to Meta and return the reusable media id."""
        url = self._url(f"{self.phone_number_id}/media")
        try:
            response = requests.post(
                url,
                headers={"Authorization": f"Bearer {self._access_token}"},
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (filename, content, mime_type)},
                timeout=30,
            )
            response.raise_for_status()
            return MediaUploadResult(media_id=response.json()["id"], success=True)
        except (requests.RequestException, KeyError, ValueError) as e:
            raise WhatsAppProviderError(f"Failed to upload media: {e}") from e

    def upload_template_media(
        self, content: bytes, mime_type: str, filename: str = "upload"
    ) -> MediaHandleResult:
        """App-scoped resumable upload, used only for a template's header media
        example (`example.header_handle`). Two steps: start an upload session
        against the app, then push the bytes to that session to get a handle.
        """
        if not self._app_id:
            raise WhatsAppProviderError("WHATSAPP_APP_ID is not configured.")
        try:
            start = requests.post(
                f"{_graph_api_base()}/{self._app_id}/uploads",
                params={
                    "file_length": len(content),
                    "file_type": mime_type,
                    "access_token": self._access_token,
                },
                timeout=30,
            )
            start.raise_for_status()
            session_id = start.json()["id"]

            push = requests.post(
                f"{_graph_api_base()}/{session_id}",
                headers={"Authorization": f"OAuth {self._access_token}"},
                data=content,
                timeout=60,
            )
            push.raise_for_status()
            return MediaHandleResult(handle=push.json()["h"], success=True)
        except (requests.RequestException, KeyError, ValueError) as e:
            raise WhatsAppProviderError(f"Failed to upload template media: {e}") from e

    def get_media_url(self, media_id: str) -> MediaUrlResult:
        """Retrieve the download URL for an uploaded media file."""
        url = self._url(media_id)
        try:
            response = self._session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            return MediaUrlResult(
                url=data["url"],
                media_type=data.get("mime_type", "application/octet-stream"),
                size_bytes=data.get("file_size"),
            )
        except requests.RequestException as e:
            raise WhatsAppProviderError(f"Failed to get media URL for {media_id}: {e}") from e

    def download_media(self, media_url: str) -> bytes:
        """Download media from a provider-supplied URL."""
        try:
            response = self._session.get(media_url, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.RequestException as e:
            raise WhatsAppProviderError(f"Failed to download media: {e}") from e

    def mark_as_read(self, message_id: str) -> ReadReceiptResult:
        """Mark an inbound message as read."""
        url = self._url(f"{self.phone_number_id}/messages")
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        try:
            response = self._session.post(url, json=payload, timeout=10)
            response.raise_for_status()
            return ReadReceiptResult(success=True)
        except requests.RequestException as e:
            logger.error(f"Failed to mark message {message_id} as read: {e}")
            return ReadReceiptResult(
                success=False,
                error=str(e),
            )
