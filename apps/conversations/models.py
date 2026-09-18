"""Phase 1 operational spine: the channel-agnostic Conversation/Message/Event
primitives that sit above channel-specific implementations (WhatsApp, and
later Email/SMS).

Design invariants (see docs/plans — "Locked Implementation Decisions"):
  - Every core object carries an explicit ``account`` FK (no relying on
    relationship traversal for tenancy).
  - ``Event`` rows are immutable facts — never mutated after creation.
  - Channel-specific state (WhatsApp's 24h session window, message IDs,
    templates) stays in ``apps.whatsapp`` models; this app only points at
    them via nullable one-to-one references. The existing WhatsApp
    implementation is preserved, not rewritten.
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models


def _conversation_public_id() -> str:
    return "conv_" + secrets.token_hex(12)


def _event_public_id() -> str:
    return "evt_" + secrets.token_hex(12)


class Conversation(models.Model):
    """A generic, channel-agnostic conversation with a single Contact.

    Wraps (rather than replaces) channel-specific conversation records, e.g.
    ``apps.whatsapp.models.Conversation`` via ``whatsapp_conversation``. Owns
    the concepts a team inbox needs regardless of channel: assignment,
    status, unread state, internal notes.
    """

    class Channel(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp"
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    public_id = models.CharField(
        max_length=40, unique=True, default=_conversation_public_id, editable=False
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="conversations"
    )
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.CASCADE, related_name="conversations"
    )
    channel = models.CharField(max_length=20, choices=Channel.choices)

    # Channel-specific backing record. Only one of these is set, matching
    # ``channel``. Nullable/one-to-one so the channel app's own lifecycle
    # (e.g. WhatsApp's 24h window) remains authoritative there.
    whatsapp_conversation = models.OneToOneField(
        "whatsapp.Conversation",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="generic_conversation",
    )

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_conversations",
    )
    is_unread = models.BooleanField(default=True)

    last_message_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "status", "last_message_at"]),
            models.Index(fields=["account", "assigned_to"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["whatsapp_conversation"],
                condition=models.Q(whatsapp_conversation__isnull=False),
                name="unique_generic_conversation_per_whatsapp_conversation",
            ),
        ]
        ordering = ["-last_message_at", "-created_at"]

    def __str__(self):
        return f"{self.get_channel_display()} conversation with {self.contact}"

    @classmethod
    def get_or_create_for_whatsapp(cls, whatsapp_conversation) -> "Conversation":
        """Idempotently get/create the generic wrapper for a whatsapp.Conversation.

        ``whatsapp_conversation.contact.contact`` (the linked ``apps.contacts.Contact``)
        must already be resolved by the caller — the generic spine only ever
        points at the canonical Contact, never at a channel identity.
        """
        contact = whatsapp_conversation.contact.contact
        if contact is None:
            raise ValueError(
                "whatsapp_conversation.contact.contact must be resolved before "
                "creating a generic Conversation"
            )
        convo, _ = cls.objects.get_or_create(
            whatsapp_conversation=whatsapp_conversation,
            defaults={
                "account": whatsapp_conversation.account,
                "contact": contact,
                "channel": cls.Channel.WHATSAPP,
            },
        )
        return convo

    def register_inbound(self, at) -> None:
        self.last_message_at = at
        self.status = self.Status.OPEN
        self.is_unread = True
        self.save(update_fields=["last_message_at", "status", "is_unread", "updated_at"])

    def assign(self, user) -> None:
        self.assigned_to = user
        self.save(update_fields=["assigned_to", "updated_at"])

    def mark_read(self) -> None:
        if self.is_unread:
            self.is_unread = False
            self.save(update_fields=["is_unread", "updated_at"])

    def close(self) -> None:
        self.status = self.Status.CLOSED
        self.save(update_fields=["status", "updated_at"])


class Message(models.Model):
    """A generic, provider-neutral message.

    Deliberately unopinionated: only universal fields live here. All
    provider-specific data (WhatsApp message IDs, templates, media, email
    threading headers) stays on the channel-specific record referenced via
    ``whatsapp_message`` (and, later, an equivalent email field).
    """

    class Direction(models.TextChoices):
        INBOUND = "inbound", "Inbound"
        OUTBOUND = "outbound", "Outbound"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="conversation_messages"
    )
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    direction = models.CharField(max_length=10, choices=Direction.choices)
    body = models.TextField(blank=True, default="")
    timestamp = models.DateTimeField()
    status = models.CharField(max_length=20, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    whatsapp_message = models.OneToOneField(
        "whatsapp.MessageLog",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="generic_message",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["conversation", "timestamp"]),
        ]
        ordering = ["timestamp"]

    def __str__(self):
        return f"{self.direction} message in conversation {self.conversation_id}"


class ConversationNote(models.Model):
    """An internal, staff-only note attached to a conversation."""

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="notes"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Note on conversation {self.conversation_id}"


class Event(models.Model):
    """A durable, immutable domain event.

    Never mutated after creation — if the world changes, a new Event is
    emitted (e.g. ``message_received`` then, later, ``message_sent``), so
    this table is an auditable history. This is distinct from:

      - ``apps.core.events`` (in-process, non-durable Django-Signal dispatch;
        kept as-is, used for immediate in-process notification alongside this
        durable record — not replaced by it).
      - ``apps.events.BusinessEvent`` (the public ``POST /v1/events`` API
        ingestion table for developer-supplied facts).

    Idempotency follows the same pattern as ``whatsapp.MessageLog``
    (``UniqueConstraint`` on the natural key) and ``whatsapp.OutboundMessage``
    (explicit idempotency key): ``(account, source, source_event_id)`` must be
    unique when ``source_event_id`` is set, so a retried webhook cannot
    produce a duplicate Event.
    """

    public_id = models.CharField(
        max_length=40, unique=True, default=_event_public_id, editable=False
    )
    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="events")
    type = models.CharField(max_length=100, db_index=True)
    occurred_at = models.DateTimeField()

    source = models.CharField(max_length=50)
    source_event_id = models.CharField(max_length=255, blank=True, default="")

    actor = models.CharField(max_length=100, blank=True, default="")
    subject_type = models.CharField(max_length=50, blank=True, default="")
    subject_id = models.CharField(max_length=64, blank=True, default="")

    payload = models.JSONField(default=dict, blank=True)
    correlation_id = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "source", "source_event_id"],
                condition=models.Q(source_event_id__gt=""),
                name="unique_event_per_account_source_event_id",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "type", "occurred_at"]),
            models.Index(fields=["account", "subject_type", "subject_id"]),
        ]
        ordering = ["occurred_at"]

    def __str__(self):
        return f"{self.type} @ {self.occurred_at.isoformat()}"
