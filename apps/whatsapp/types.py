"""WhatsApp provider result types.

All return values from WhatsAppProvider methods use these typed dataclasses,
not raw dicts. This ensures type safety across provider implementations and
makes adapters (dict → typed result) the provider's responsibility, not the
business logic's.
"""

from dataclasses import dataclass


@dataclass
class SendResult:
    """Result of a message send operation."""

    message_id: str
    """Meta Cloud API message ID — immutable across retries."""

    success: bool
    """Whether the message was accepted for delivery."""

    error: str | None = None
    """Error message if success=False (e.g., 'invalid_recipient', 'rate_limit')."""

    error_code: str | None = None
    """Provider error code (e.g. Meta '131047') when success=False."""

    retryable: bool = True
    """Whether re-attempting the send could succeed. False for policy/permanent errors."""

    metadata: dict = None
    """Extra data from the provider (e.g., timestamp, cost, etc.)."""

    ambiguous: bool = False
    """The request may have reached Meta (e.g. a read timeout): it might have been sent, so it
    must not be sent again automatically."""

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class MediaUploadResult:
    """Result of uploading a local media file to the provider."""

    media_id: str
    """Provider media id to reference in a subsequent send_media() call."""

    success: bool = True

    error: str | None = None


@dataclass
class MediaHandleResult:
    """Result of Meta's app-scoped resumable upload, used for template header media.

    Distinct from MediaUploadResult: that one is phone-number-scoped and used
    to send a message; this handle is app-scoped and referenced in a template
    creation payload's `example.header_handle`.
    """

    handle: str
    """Opaque handle referencing the uploaded file for template creation."""

    success: bool = True

    error: str | None = None


@dataclass
class MediaUrlResult:
    """Result of a media URL retrieval operation."""

    url: str
    """Direct download URL for the media file."""

    media_type: str
    """MIME type (e.g., 'image/jpeg', 'video/mp4')."""

    size_bytes: int | None = None
    """File size in bytes if available from provider."""


@dataclass
class ReadReceiptResult:
    """Result of marking a message as read."""

    success: bool
    """Whether the read receipt was accepted."""

    error: str | None = None
    """Error message if success=False."""
