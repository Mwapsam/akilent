from datetime import timedelta

from django.db import models
from django.utils import timezone

from .contact import WhatsAppContact
from .message import MessageLog
from .templates import MessageTemplate


class OutboundMessage(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        # Not attempted: the plan's message limit was reached. Not a failure.
        HELD = "held", "Held (plan limit)"
        # The request may have reached Meta but no answer came back. Never resent automatically.
        UNCONFIRMED = "unconfirmed", "May not have been sent"

    MAX_ATTEMPTS = 5

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE)
    contact = models.ForeignKey(WhatsAppContact, on_delete=models.CASCADE)

    template = models.ForeignKey(
        MessageTemplate, blank=True, null=True, on_delete=models.SET_NULL
    )

    payload = models.JSONField()
    idempotency_key = models.CharField(max_length=255, blank=True, null=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.QUEUED
    )
    error_code = models.CharField(max_length=32, blank=True, default="")

    scheduled_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True, null=True)

    message_log = models.OneToOneField(
        MessageLog,
        blank=True,
        null=True,
        on_delete=models.SET_NULL,
        related_name="outbound_source",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),  # drain worker
            models.Index(fields=["account", "status"]),  # rate limiter
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "idempotency_key"],
                condition=models.Q(idempotency_key__isnull=False),
                name="unique_outbound_idempotency",
            ),
        ]

    def mark_failed(self, error: str, *, terminal: bool = False, error_code: str = ""):
        """Record a send failure.

        ``terminal=True`` (policy violations, non-retryable provider errors)
        jumps straight to FAILED without consuming a retry attempt or scheduling
        a backoff. Otherwise the message is re-queued with exponential backoff
        until ``MAX_ATTEMPTS`` is reached.
        """
        self.attempts += 1
        self.last_error = error[:5000]
        if error_code:
            self.error_code = error_code[:32]
        if terminal or self.attempts >= self.MAX_ATTEMPTS:
            self.status = self.Status.FAILED
            self.next_attempt_at = None
        else:
            # Exponential backoff: 1, 2, 4, 8 minutes…
            self.status = self.Status.QUEUED
            self.next_attempt_at = timezone.now() + timedelta(
                minutes=2 ** (self.attempts - 1)
            )
        self.save(
            update_fields=[
                "attempts",
                "last_error",
                "status",
                "next_attempt_at",
                "error_code",
            ]
        )

    def __str__(self):
        return f"-> {self.contact.phone_number} [{self.status}]"
