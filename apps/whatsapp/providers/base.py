"""Provider-agnostic WhatsAppProvider interface.

Any WhatsApp-compatible backend (Meta Cloud API, Twilio, Vonage, ...)
is supported by implementing this ABC. Business logic imports only from here
and from apps.whatsapp.types — never from a concrete provider module.

Design principle: every method returns a typed dataclass from apps.whatsapp.types,
never a raw dict. Adapters belong in the provider, not scattered across the
service layer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from apps.whatsapp.types import (
    MediaHandleResult,
    MediaUploadResult,
    MediaUrlResult,
    ReadReceiptResult,
    SendResult,
)


class WhatsAppProvider(ABC):
    """Abstract interface for a WhatsApp-compatible messaging backend.

    Implement all abstract methods to add a new provider. The factory in
    apps.whatsapp.providers.__init__ resolves the concrete class at runtime.

    Threading: instances are not thread-safe. Instantiate one per request,
    per Celery task, or per service call.
    """

    @abstractmethod
    def send_text(self, to: str, body: str) -> SendResult:
        """Send a plain text message.

        Args:
            to: Recipient phone number (with or without + prefix).
            body: Plain text message body.

        Returns:
            SendResult with message_id and success status.

        Raises:
            WhatsAppProviderError: on network failure, auth error, etc.
        """

    @abstractmethod
    def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        components: list,
    ) -> SendResult:
        """Send a pre-approved template message.

        Args:
            to: Recipient phone number (with or without + prefix).
            template_name: Name of the approved template.
            language: ISO 639-1 language code (e.g., 'en').
            components: List of template component dicts from Meta's schema.

        Returns:
            SendResult with message_id and success status.

        Raises:
            WhatsAppProviderError: on network failure, auth error, etc.
        """

    @abstractmethod
    def send_media(
        self,
        to: str,
        media_type: str,
        media_id: str,
        caption: str = "",
    ) -> SendResult:
        """Send a media message (image, video, document, audio).

        Args:
            to: Recipient phone number (with or without + prefix).
            media_type: 'image', 'video', 'document', or 'audio'.
            media_id: Provider's media ID (already uploaded).
            caption: Optional caption for image/video (ignored for audio/document).

        Returns:
            SendResult with message_id and success status.

        Raises:
            WhatsAppProviderError: on network failure, auth error, etc.
        """

    @abstractmethod
    def get_media_url(self, media_id: str) -> MediaUrlResult:
        """Retrieve the download URL for an uploaded media file.

        Args:
            media_id: Provider's media ID from an inbound message.

        Returns:
            MediaUrlResult with downloadable URL.

        Raises:
            WhatsAppProviderError: on network failure, auth error, etc.
        """

    @abstractmethod
    def download_media(self, media_url: str) -> bytes:
        """Download media from a provider-supplied URL.

        Args:
            media_url: URL returned by get_media_url().

        Returns:
            Raw bytes of the media file.

        Raises:
            WhatsAppProviderError: on network failure, etc.
        """

    def send_interactive(self, to: str, interactive: dict) -> SendResult:
        """Send an interactive message (reply buttons or a list).

        Optional capability. ``interactive`` is Meta's ``interactive`` object, built by
        ``apps.whatsapp.interactive`` (which enforces WhatsApp's limits). Like free text it is
        only deliverable inside the 24-hour customer-service window.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support interactive messages"
        )

    def list_templates(self, waba_id: str) -> list[dict]:
        """List message templates for a WhatsApp Business Account.

        Optional capability. Returns Meta's raw template dicts
        (name, language, category, status, components, id).
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support template listing"
        )

    def create_template(self, waba_id: str, payload: dict) -> dict:
        """Submit a new message template for approval.

        Optional capability. ``payload`` is Meta's template-creation schema
        (name/language/category/components) — callers build it via
        ``apps.whatsapp.template_builder``, never construct it inline.
        Returns Meta's raw response (carries the new template's id and status,
        normally "PENDING").
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support template creation"
        )

    def upload_media(self, content: bytes, mime_type: str, filename: str = "upload") -> MediaUploadResult:
        """Upload a local media file and return a reusable provider media id.

        Optional capability — providers that cannot upload raise
        NotImplementedError. Callers should pre-upload and then call send_media()
        with the returned id.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support media upload"
        )

    def upload_template_media(
        self, content: bytes, mime_type: str, filename: str = "upload"
    ) -> MediaHandleResult:
        """Upload a local file and return a Meta app-scoped handle for use in a
        template creation payload's `example.header_handle`.

        Optional capability — distinct from upload_media(), which is
        phone-number-scoped and used for sending messages, not template
        creation. Providers that cannot upload raise NotImplementedError.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support template media upload"
        )

    @abstractmethod
    def mark_as_read(self, message_id: str) -> ReadReceiptResult:
        """Mark an inbound message as read.

        Args:
            message_id: Meta message ID from an inbound webhook.

        Returns:
            ReadReceiptResult indicating success or failure.

        Raises:
            WhatsAppProviderError: on network failure, auth error, etc.
        """


class WhatsAppProviderError(Exception):
    """Base exception for WhatsApp provider errors."""

    pass
