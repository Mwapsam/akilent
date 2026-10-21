import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone

from .outbound import OutboundMessage


class WhatsAppCampaign(models.Model):
    """One bulk WhatsApp template send to a Contact List — the WhatsApp side of
    the channel-neutral "Campaigns" concept (R1.5c).

    Fanned out via ``WhatsAppCampaignRecipient`` (one row per contact, created
    up front) and a bounded, self-rescheduling chunk task
    (``apps.whatsapp.campaigns.send_campaign``), mirroring
    ``apps.email.models.BulkEmailCampaign``/``BulkEmailRecipient`` — restart-safe
    for lists of thousands of contacts, unlike a single inline loop.

    ``queued_count``/``skipped_count`` here are the *processing* summary (has
    Akilent gotten to this recipient yet, and what did it decide) — never
    delivery status. Delivery (sent/delivered/read/failed at Meta) is read
    live from ``OutboundMessage``/``MessageLog`` via
    ``apps.whatsapp.campaigns.campaign_delivery_stats``, a deliberately
    separate query so the two concepts never blend into one number.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        # Waiting for scheduled_at; recipients are only chosen when it fires.
        SCHEDULED = "scheduled", "Scheduled"
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        # Fan-out stops at the next chunk and the outbound queue holds back its
        # messages until it's resumed.
        PAUSED = "paused", "Paused"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    # Recipients exist (or are being created) only from QUEUED on; a draft or a
    # scheduled campaign is still editable.
    EDITABLE = (Status.DRAFT, Status.SCHEDULED)
    ACTIVE = (Status.QUEUED, Status.SENDING)
    FINISHED = (Status.COMPLETED, Status.FAILED, Status.CANCELLED)

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="whatsapp_campaigns"
    )
    name = models.CharField(max_length=150)
    # The audience: exactly one of contact_list / segment. A segment is
    # evaluated when the campaign starts, so it reaches whoever matches then.
    contact_list = models.ForeignKey(
        "contacts.ContactList",
        on_delete=models.PROTECT,
        related_name="whatsapp_campaigns",
        null=True,
        blank=True,
    )
    segment = models.ForeignKey(
        "contacts.Segment",
        on_delete=models.PROTECT,
        related_name="whatsapp_campaigns",
        null=True,
        blank=True,
    )
    # "Retry failed": this campaign goes only to the customers whose message
    # failed in that one (still excluding anyone who has since opted out).
    retry_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        related_name="retries",
        null=True,
        blank=True,
    )
    template = models.ForeignKey(
        "whatsapp.MessageTemplate", on_delete=models.PROTECT, related_name="campaigns"
    )
    # {"<template variable>": "contact.<attribute>" | "account.<attr>" |
    # "business.<fact>" | literal string} — resolved by
    # apps.automation.variables.resolve, the same resolver the workflow
    # editor's send_whatsapp step and engagement starters use.
    variable_mapping = models.JSONField(default=dict, blank=True)
    # {"<template variable>": "<text to use when this contact has no value>"}.
    variable_fallbacks = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    recipient_count = models.PositiveIntegerField(default=0)
    queued_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    # Touched every time send_campaign claims/dispatches a chunk for this
    # campaign — the signal apps.whatsapp.tasks.sweep_stuck_campaigns uses to
    # tell "still working, just a big list" from "the chunk chain died"
    # (e.g. an on_commit-scheduled .delay() that never reached the broker).
    last_progress_at = models.DateTimeField(blank=True, null=True)
    scheduled_at = models.DateTimeField(blank=True, null=True)
    # The IANA zone the owner picked the time in, so the screens show it back
    # the way they typed it. scheduled_at itself is UTC.
    timezone = models.CharField(max_length=64, blank=True, default="")
    # When recipients were chosen from the list/segment (at send, not creation).
    audience_snapshot_at = models.DateTimeField(blank=True, null=True)
    # [{"at": iso, "event": "scheduled", "by": "Ada"}...] — the detail page's
    # timeline. Append-only; see record().
    history = models.JSONField(default=list, blank=True)
    # How the sends are spread out, decided when it starts (apps.whatsapp.
    # campaigns.send_plan_for): Meta's daily limit for the number, the room left
    # today, and the business's quiet hours. Empty = send as fast as allowed.
    send_plan = models.JSONField(default=dict, blank=True)
    # One per composer page load: a double-click or re-submitted form carries the
    # same key and gets the campaign already created instead of a second
    # broadcast to the same people.
    submission_key = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["status", "scheduled_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "submission_key"],
                condition=~models.Q(submission_key=""),
                name="unique_whatsapp_campaign_submission",
            ),
        ]

    @property
    def audience_name(self) -> str:
        if self.segment_id:
            return self.segment.name  # type: ignore[union-attr]
        if self.contact_list_id:
            return self.contact_list.name  # type: ignore[union-attr]
        return ""

    def record(self, event: str, user=None, *, save: bool = True, **extra) -> None:
        """Append one timeline entry (who did what, when)."""
        entry = {"at": timezone.now().isoformat(), "event": event}
        if user is not None and getattr(user, "is_authenticated", False):
            entry["by"] = user.get_full_name() or user.get_username()
        entry.update(extra)
        self.history = [*(self.history or []), entry]
        if save:
            self.save(update_fields=["history"])

    def mark_sending(self) -> None:
        """Idempotent: called at the start of every chunk, but only the first
        call actually transitions status/sets started_at."""
        if self.status not in (self.Status.DRAFT, self.Status.QUEUED):
            return
        first = self.started_at is None
        # Conditional, so a pause or cancel that landed since this copy was
        # loaded is never overwritten back to Sending.
        moved = (
            type(self)
            .objects.filter(
                pk=self.pk, status__in=[self.Status.DRAFT, self.Status.QUEUED]
            )
            .update(
                status=self.Status.SENDING, started_at=self.started_at or timezone.now()
            )
        )
        self.refresh_from_db(fields=["status", "started_at"])
        if moved and first:
            self.record("started")

    def touch_progress(self) -> None:
        self.last_progress_at = timezone.now()
        self.save(update_fields=["last_progress_at"])

    def mark_completed(self) -> None:
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.record("completed", save=False)
        self.save(update_fields=["status", "completed_at", "history"])

    def mark_failed(self, error: str) -> None:
        self.status = self.Status.FAILED
        self.error = error[:5000]
        self.completed_at = timezone.now()
        self.record("failed", save=False)
        self.save(update_fields=["status", "error", "completed_at", "history"])

    def increment_counts(self, *, queued: int = 0, skipped: int = 0) -> None:
        """Processing summary only — never delivery status. Uses F() expressions
        so concurrent chunk writes (across different campaigns) can't race."""
        updates = []
        if queued:
            self.queued_count = models.F("queued_count") + queued
            updates.append("queued_count")
        if skipped:
            self.skipped_count = models.F("skipped_count") + skipped
            updates.append("skipped_count")
        if updates:
            self.save(update_fields=updates)
            self.refresh_from_db(fields=updates)

    def __str__(self):
        return f"WhatsApp campaign {self.name!r} ({self.status})"


class WhatsAppCampaignRecipient(models.Model):
    """One contact's fan-out row for a WhatsAppCampaign — processing state only.

    Distinct from delivery: ``status`` here answers "did Akilent act on this
    recipient, and how" (never sent to Meta / created a message / deliberately
    skipped). What Meta reported back about a QUEUED recipient's message lives
    on ``message.status``/``message.message_log.status``, never here — see
    ``apps.whatsapp.campaigns.campaign_delivery_stats``.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"
        HELD = "held", "Held"

    class SkipReason(models.TextChoices):
        OPTED_OUT = "opted_out", "Opted out"
        NO_WHATSAPP_IDENTITY = "no_whatsapp_identity", "No WhatsApp number"
        MISSING_VALUE = "missing_value", "Missing template value"
        # A marketing template only goes to people who agreed to hear from the
        # business (Meta policy; enforced again at send time in _authorize_send).
        NOT_OPTED_IN = "not_opted_in", "Hasn't opted in"
        CANCELLED = "cancelled", "Campaign cancelled"
        # Held by the plan's monthly limit for too long; not sent days late.
        PLAN_LIMIT = "plan_limit", "Plan limit — not sent late"
        # WhatsApp told us this number isn't on WhatsApp.
        NOT_ON_WHATSAPP = "not_on_whatsapp", "Not on WhatsApp"
        # Meta paused or rejected the template while the campaign was sending.
        TEMPLATE_STOPPED = "template_stopped", "Template stopped by Meta"

    campaign = models.ForeignKey(
        WhatsAppCampaign, on_delete=models.CASCADE, related_name="recipients"
    )
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE)
    message = models.OneToOneField(
        OutboundMessage,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="campaign_recipient",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    skip_reason = models.CharField(
        max_length=25, choices=SkipReason.choices, blank=True, default=""
    )
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "contact"],
                name="unique_whatsapp_campaign_contact",
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"]),
            models.Index(fields=["campaign", "skip_reason"]),
        ]

    def __str__(self):
        return f"{self.campaign_id}:{self.contact_id} [{self.status}]"


class CampaignSendWindow(models.Model):
    """The hours a business lets campaign messages arrive (quiet hours).

    In the business's own timezone (apps.accounts.business_hours.
    workspace_timezone). A campaign message due outside the window waits for
    the next opening. Replies and other one-off messages are never held.
    """

    account = models.OneToOneField(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="campaign_send_window",
    )
    enabled = models.BooleanField(default=True)
    start = models.TimeField(default=datetime.time(8, 0))
    end = models.TimeField(default=datetime.time(20, 0))
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Send window {self.start}-{self.end} for {self.account_id}"
