from datetime import timedelta

from django.db import models
from django.utils import timezone


class InstagramMessage(models.Model):
    """
    An individual message within an Instagram DM conversation or linked to a
    comment thread (e.g. a public comment reply).

    Exactly one of ``conversation`` or ``comment_thread`` must be set — enforced
    by the DB CheckConstraint below.
    """

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        FAILED = "failed", "Failed"

    # Exactly one parent — DM conversation OR comment thread
    conversation = models.ForeignKey(
        "instagram.InstagramConversation",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="messages",
    )
    comment_thread = models.ForeignKey(
        "instagram.CommentThread",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="messages",
    )

    # Instagram's external message ID — unique per account (base64, ~200+ chars)
    message_id = models.CharField(max_length=500)
    direction = models.CharField(max_length=10, choices=Direction.choices)
    body = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20, choices=Status.choices, blank=True, default=""
    )
    timestamp = models.DateTimeField()
    metadata = models.JSONField(default=dict, blank=True)

    # First attachment of an inbound message (photo, video, voice note, file).
    # Meta sends a short-lived CDN URL; the bytes are copied into our storage by
    # ``tasks.download_instagram_media`` so they stay viewable in the inbox.
    media_type = models.CharField(max_length=20, blank=True, default="")
    media_source_url = models.TextField(blank=True, default="")
    media_mime_type = models.CharField(max_length=100, blank=True, default="")
    media_file = models.FileField(
        upload_to="instagram/media/%Y/%m/", blank=True, null=True
    )
    media_size = models.PositiveIntegerField(blank=True, null=True)
    media_attempts = models.PositiveSmallIntegerField(default=0)
    media_error = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(conversation__isnull=False, comment_thread__isnull=True)
                    | models.Q(conversation__isnull=True, comment_thread__isnull=False)
                ),
                name="instagram_message_exactly_one_parent",
            ),
            models.UniqueConstraint(
                fields=["message_id"],
                name="unique_instagram_message_id",
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "timestamp"]),
        ]

    def __str__(self):
        return f"InstagramMessage {self.message_id} ({self.direction})"


class OutboundMessage(models.Model):
    """
    Durable outbox for all Instagram outbound sends (DM replies, AI replies,
    private replies to comments).

    The record is created BEFORE the API call. The provider returns a SendResult
    and services/outbound.py applies the state transition — the provider never
    mutates this model directly.
    """

    MAX_ATTEMPTS = 5

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        UNCONFIRMED = "unconfirmed", "May not have been sent"

    class ActionType(models.TextChoices):
        DM_REPLY = "dm_reply", "DM Reply"
        COMMENT_REPLY = "comment_reply", "Comment Reply"
        PRIVATE_REPLY = "private_reply", "Private Reply to Comment"

    instagram_account = models.ForeignKey(
        "instagram.InstagramBusinessAccount",
        on_delete=models.CASCADE,
        related_name="outbound_messages",
    )
    # Deterministic idempotency key: {account_id}:{action_type}:{source_event_id}:{proposal_id}
    idempotency_key = models.CharField(max_length=255, unique=True)
    recipient_igsid = models.CharField(max_length=50)
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    body = models.TextField()

    # An attachment sent from the inbox (photo, video, voice note, PDF), already in
    # our storage. Meta fetches it from a short-lived signed link at send time.
    media_path = models.CharField(max_length=500, blank=True, default="")
    media_mime_type = models.CharField(max_length=100, blank=True, default="")
    media_kind = models.CharField(max_length=20, blank=True, default="")

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED
    )
    # Message ID returned by Meta after a successful send
    provider_message_id = models.CharField(max_length=500, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(blank=True, null=True)

    # Instagram quick-reply chips sent with a DM. Shape per chip:
    # {"content_type": "text", "title": str, "payload": str}
    # Max 13 chips, title ≤ 20 chars, payload ≤ 1000 chars (A workstream).
    quick_replies = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["instagram_account", "status"]),
        ]

    def mark_sent(self, provider_message_id: str):
        self.status = self.Status.SENT
        self.provider_message_id = provider_message_id
        self.sent_at = timezone.now()
        self.save(update_fields=["status", "provider_message_id", "sent_at"])

    def mark_failed(self, error: str, *, terminal: bool = False):
        self.attempts += 1
        self.last_error = error[:5000]
        if terminal or self.attempts >= self.MAX_ATTEMPTS:
            self.status = self.Status.FAILED
            self.next_attempt_at = None
        else:
            self.status = self.Status.QUEUED
            self.next_attempt_at = timezone.now() + timedelta(
                minutes=2 ** (self.attempts - 1)
            )
        self.save(update_fields=["attempts", "last_error", "status", "next_attempt_at"])

    def __str__(self):
        return f"-> {self.recipient_igsid} [{self.status}]"
