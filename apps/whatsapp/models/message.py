from django.db import models

from .contact import WhatsAppContact
from .conversation import Conversation


class MessageLog(models.Model):
    class Direction(models.TextChoices):
        INBOUND = "in", "Inbound"
        OUTBOUND = "out", "Outbound"

    class MessageType(models.TextChoices):
        TEXT = "text", "Text"
        IMAGE = "image", "Image"
        AUDIO = "audio", "Audio"
        VIDEO = "video", "Video"
        DOCUMENT = "document", "Document"
        STICKER = "sticker", "Sticker"
        LOCATION = "location", "Location"
        CONTACTS = "contacts", "Contacts"
        TEMPLATE = "template", "Template"
        UNKNOWN = "unknown", "Unknown"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        FAILED = "failed", "Failed"
        HELD = "held", "Held (plan limit)"
        UNCONFIRMED = "unconfirmed", "May not have been sent"

    _STATUS_RANK = {
        Status.QUEUED: 0,
        Status.UNCONFIRMED: 0,  # a later delivery update, if any, still wins
        Status.SENT: 1,
        Status.DELIVERED: 2,
        Status.READ: 3,
        Status.HELD: 98,
        Status.FAILED: 99,
    }

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    contact = models.ForeignKey(WhatsAppContact, on_delete=models.CASCADE)
    direction = models.CharField(max_length=10, choices=Direction.choices)
    message_id = models.CharField(max_length=255, blank=True, null=True)
    message_type = models.CharField(
        max_length=20, choices=MessageType.choices, default=MessageType.TEXT
    )

    content = models.TextField(blank=True, default="")
    media_id = models.CharField(max_length=255, blank=True, null=True)
    media_url = models.URLField(max_length=1000, blank=True, null=True)
    media_mime_type = models.CharField(max_length=100, blank=True, null=True)
    media_file = models.FileField(
        upload_to="whatsapp/media/%Y/%m/", blank=True, null=True
    )
    media_size = models.PositiveIntegerField(blank=True, null=True)
    media_attempts = models.PositiveSmallIntegerField(default=0)
    media_error = models.CharField(max_length=500, blank=True, default="")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED
    )

    timestamp = models.DateTimeField()  # WhatsApp-reported time

    raw_payload = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["timestamp"]),
            models.Index(fields=["conversation", "timestamp"]),
            models.Index(fields=["contact", "timestamp", "direction"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "message_id"],
                condition=models.Q(message_id__isnull=False),
                name="unique_message_per_account",
            ),
        ]

    def apply_status_update(self, new_status: str) -> bool:
        current = self._STATUS_RANK.get(self.status, 0)
        incoming = self._STATUS_RANK.get(new_status)
        if incoming is None or incoming <= current:
            return False
        self.status = new_status
        self.save(update_fields=["status"])
        return True

    def __str__(self):
        return f"[{self.direction}] {self.contact.phone_number} @ {self.timestamp:%Y-%m-%d %H:%M}"
