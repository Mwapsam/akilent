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

    def register_outbound(self, at) -> None:
        """Advance ``last_message_at`` for a business message. Unlike an inbound message it
        neither re-opens the conversation nor marks it unread."""
        if self.last_message_at is None or at > self.last_message_at:
            self.last_message_at = at
            self.save(update_fields=["last_message_at", "updated_at"])

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
        # Neither party speaking: a fact about the customer's story that belongs
        # in the thread ("Payment received"). Deliberately matches neither
        # direction filter in apps.conversations.state, so it can never make a
        # waiting customer look answered.
        SYSTEM = "system", "System"

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


class SavedReply(models.Model):
    """A reusable, plain-text canned reply a business writes once for its
    agents (R2.1). Deliberately no variable substitution or channel scoping —
    that's what a WhatsApp ``MessageTemplate`` is for; this is only ever
    inserted into the free-text composer draft, never sent on its own."""

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="saved_replies"
    )
    title = models.CharField(max_length=100)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title


class FollowUp(models.Model):
    """A due-date reminder to return to a customer (R2.2). Deliberately not a
    general task system — creation is limited to "remind me in 1h / tomorrow /
    pick a time" from a conversation, per the plan's UX guardrail."""

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="followups")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="followups")
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="followups", null=True, blank=True
    )
    due_at = models.DateTimeField()
    note = models.CharField(max_length=255, blank=True, default="")
    done_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "done_at", "due_at"]),
        ]
        ordering = ["due_at"]

    def __str__(self):
        return f"Follow up with {self.contact} at {self.due_at.isoformat()}"

    def mark_done(self) -> None:
        from django.utils import timezone

        if self.done_at is None:
            self.done_at = timezone.now()
            self.save(update_fields=["done_at"])


class ConversationAttribution(models.Model):
    """Why Akilent credited a lead, deal or order to a conversation. Immutable.

    Exactly one of ``lead`` / ``deal`` / ``order`` is set, and each object gets at most one
    record, made when the object is created. A correction is a new record on a new object, never
    an edit, so a report run next year says the same as today. ``Lead/Deal/Order.conversation``
    stay as fast shortcuts to the same answer; this is the record of how it was decided.
    """

    class Method(models.TextChoices):
        EXPLICIT = "explicit", "Explicit"                      # a person or workflow named the conversation
        RECENT_CONVERSATION = "recent_conversation", "Recent conversation"  # customer's latest chat, 30 days

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="attributions")
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="attributions")
    channel = models.CharField(max_length=20, choices=Conversation.Channel.choices)
    lead = models.OneToOneField(
        "crm.Lead", on_delete=models.CASCADE, null=True, blank=True, related_name="attribution")
    deal = models.OneToOneField(
        "crm.Deal", on_delete=models.CASCADE, null=True, blank=True, related_name="attribution")
    order = models.OneToOneField(
        "commerce.Order", on_delete=models.CASCADE, null=True, blank=True, related_name="attribution")
    workflow_run = models.ForeignKey(
        "automation.WorkflowRun", on_delete=models.SET_NULL, null=True, blank=True, related_name="attributions")
    method = models.CharField(max_length=24, choices=Method.choices)
    attributed_at = models.DateTimeField(auto_now_add=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        indexes = [models.Index(fields=["account", "channel", "attributed_at"])]
        constraints = [
            models.CheckConstraint(
                name="attribution_exactly_one_subject",
                condition=(
                    models.Q(lead__isnull=False, deal__isnull=True, order__isnull=True)
                    | models.Q(lead__isnull=True, deal__isnull=False, order__isnull=True)
                    | models.Q(lead__isnull=True, deal__isnull=True, order__isnull=False)
                ),
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("An attribution is a historical record and cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("An attribution is a historical record and cannot be deleted.")
