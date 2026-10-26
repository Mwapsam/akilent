from datetime import timedelta

from django.db import models
from django.utils import timezone


class FacebookMessage(models.Model):
    """
    An individual Messenger message, or a business reply linked to a comment thread.

    Exactly one of ``conversation`` or ``comment_thread`` is set — enforced by the
    CheckConstraint below.
    """

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        READ = "read", "Read"
        FAILED = "failed", "Failed"

    conversation = models.ForeignKey(
        "facebook.FacebookConversation",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="messages",
    )
    comment_thread = models.ForeignKey(
        "facebook.FacebookCommentThread",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="messages",
    )

    # Messenger's message id (mid)
    message_id = models.CharField(max_length=500)
    direction = models.CharField(max_length=10, choices=Direction.choices)
    body = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20, choices=Status.choices, blank=True, default=""
    )
    timestamp = models.DateTimeField()
    metadata = models.JSONField(default=dict, blank=True)

    # First attachment of an inbound message. Meta sends a short-lived CDN URL; the
    # bytes are copied into our storage by ``tasks.download_facebook_media``.
    media_type = models.CharField(max_length=20, blank=True, default="")
    media_source_url = models.TextField(blank=True, default="")
    media_mime_type = models.CharField(max_length=100, blank=True, default="")
    media_file = models.FileField(
        upload_to="facebook/media/%Y/%m/", blank=True, null=True
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
                name="facebook_message_exactly_one_parent",
            ),
            models.UniqueConstraint(
                fields=["message_id"],
                name="unique_facebook_message_id",
            ),
        ]
        indexes = [
            models.Index(fields=["conversation", "timestamp"]),
        ]

    def __str__(self):
        return f"FacebookMessage {self.message_id} ({self.direction})"


class FacebookOutboundMessage(models.Model):
    """
    Durable outbox for every Facebook send: Messenger replies, private replies to
    comments, public comment replies and new comments on the Page's posts.

    The record is created BEFORE the API call; services/outbound.py applies the
    state transition from the provider's SendResult.
    """

    MAX_ATTEMPTS = 5

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        UNCONFIRMED = "unconfirmed", "May not have been sent"

    class ActionType(models.TextChoices):
        DM_REPLY = "dm_reply", "Messenger reply"
        COMMENT_REPLY = "comment_reply", "Comment Reply"
        PRIVATE_REPLY = "private_reply", "Private Reply to Comment"
        POST_COMMENT = "post_comment", "Comment on a post"

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="facebook_outbound_messages",
    )
    page_id = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=255, unique=True)
    # A PSID for Messenger replies; a comment id for comment and private replies; a
    # post id for a new comment on a post.
    recipient_id = models.CharField(max_length=100)
    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    body = models.TextField()

    media_path = models.CharField(max_length=500, blank=True, default="")
    media_mime_type = models.CharField(max_length=100, blank=True, default="")
    media_kind = models.CharField(max_length=20, blank=True, default="")

    # Quick-reply chips: {"content_type": "text", "title": str, "payload": str}
    quick_replies = models.JSONField(default=list, blank=True)

    # Typed by a person in the inbox: may use the HUMAN_AGENT tag after 24 hours.
    human_agent = models.BooleanField(default=False)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED
    )
    provider_message_id = models.CharField(max_length=500, blank=True, default="")
    # The PSID Meta returned for the send. A private reply's recipient is a comment, and
    # the commenter's Messenger PSID is only known from this.
    resolved_recipient_id = models.CharField(max_length=100, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True, default="")
    sent_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["account", "status"]),
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
        return f"-> {self.recipient_id} [{self.status}]"
