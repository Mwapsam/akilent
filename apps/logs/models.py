"""Observability data stores.

Phase 0 / Epic P0.1: ``MessageEvent`` — an append-only, per-message lifecycle
log. It is the single source of truth for "what happened to this email", and
replaces reliance on the destructive ``EmailMessage.status`` column. Every
write goes through ``apps.logs.services.record_message_event`` which owns the
fan-out to outbound webhooks, analytics rollups, contact activity and workflow
triggers.
"""

from __future__ import annotations

import secrets

from django.db import models


def _event_public_id() -> str:
    return "evt_" + secrets.token_hex(16)


def _request_public_id() -> str:
    return "req_" + secrets.token_hex(16)


class MessageEvent(models.Model):
    """One lifecycle event for an :class:`~apps.email.models.EmailMessage`.

    Append-only: application code never updates or deletes rows here except the
    retention prune task.
    """

    class Type(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        QUEUED = "queued", "Queued"
        RENDERED = "rendered", "Rendered"
        SENT = "sent", "Sent"
        DELIVERED = "delivered", "Delivered"
        DEFERRED = "deferred", "Deferred"
        BOUNCED = "bounced", "Bounced"
        COMPLAINED = "complained", "Complained"
        REJECTED = "rejected", "Rejected"
        OPENED = "opened", "Opened"
        CLICKED = "clicked", "Clicked"
        FAILED = "failed", "Failed"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"

    class Source(models.TextChoices):
        PIPELINE = "pipeline", "Send pipeline"
        SES_SNS = "ses_sns", "SES / SNS notification"
        TRACKING_PIXEL = "tracking_pixel", "Open-tracking pixel"
        TRACKING_LINK = "tracking_link", "Click-tracking link"
        API = "api", "API"
        SANDBOX = "sandbox", "Sandbox provider"

    public_id = models.CharField(
        max_length=40, unique=True, default=_event_public_id, editable=False
    )
    message = models.ForeignKey(
        "email_service.EmailMessage",
        on_delete=models.CASCADE,
        related_name="events",
    )
    # Denormalized for account-scoped queries without a join through message.
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="message_events"
    )

    type = models.CharField(max_length=20, choices=Type.choices)
    source = models.CharField(max_length=20, choices=Source.choices)

    # Event time (when it happened upstream), distinct from the row insert time.
    occurred_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)

    # Provider / handler payload: SES bounceType/bounceSubType/diagnosticCode/
    # feedbackType/reportingMTA; tracking ip/ua/url; failure error/retry_count.
    data = models.JSONField(default=dict, blank=True)

    # SNS MessageId / SES mail.messageId — lets the SNS handler dedupe and lets
    # us join provider events back to their notification.
    provider_event_id = models.CharField(
        max_length=255, blank=True, null=True, db_index=True
    )

    request_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["occurred_at", "id"]
        indexes = [
            models.Index(fields=["message", "occurred_at"]),
            models.Index(fields=["account", "type", "occurred_at"]),
        ]

    def __str__(self):
        return f"{self.type} (message {self.message_id})"


class ApiRequest(models.Model):
    """One handled public-API call — the developer-facing request log."""

    public_id = models.CharField(
        max_length=40, unique=True, default=_request_public_id, editable=False
    )
    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="api_requests",
        blank=True,
        null=True,
    )
    api_key = models.ForeignKey(
        "email_service.EmailApiKey",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    key_mode = models.CharField(max_length=10, blank=True, default="")

    method = models.CharField(max_length=10)
    path = models.CharField(max_length=255)
    version = models.CharField(max_length=10, blank=True, default="")
    status_code = models.PositiveIntegerField()
    error_code = models.CharField(max_length=64, blank=True, default="")

    request_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    idempotency_key = models.CharField(max_length=255, blank=True, default="")
    idempotency_replayed = models.BooleanField(default=False)

    latency_ms = models.PositiveIntegerField(default=0)

    request_headers = models.JSONField(default=dict, blank=True)
    request_body = models.JSONField(default=dict, blank=True)
    response_body = models.JSONField(default=dict, blank=True)

    client_ip = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    # Session / release correlation (Tier-2 observability)
    browser_session_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
    )
    release = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="RELEASE_VERSION at request time.",
    )
    op_meta = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Server-generated structured diagnostic context only. "
            "Never copy request.POST or request.data here."
        ),
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "created_at"]),
            models.Index(fields=["status_code", "created_at"]),
            models.Index(fields=["browser_session_id", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.method} {self.path} -> {self.status_code}"


class IdempotencyRecord(models.Model):
    """Dedupes retried POSTs carrying an ``Idempotency-Key`` header."""

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="idempotency_records"
    )
    key = models.CharField(max_length=255)
    endpoint = models.CharField(max_length=128)
    request_fingerprint = models.CharField(max_length=64)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PROCESSING
    )
    response_status = models.PositiveIntegerField(blank=True, null=True)
    response_body = models.JSONField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "key"], name="uniq_idempotency_account_key"
            )
        ]
        indexes = [models.Index(fields=["created_at"])]

    def __str__(self):
        return f"{self.endpoint} {self.key} [{self.status}]"


class BrowserSession(models.Model):
    """One pseudonymous browser session — the common correlation key for
    frontend events, API requests, and Sentry errors.

    Created by ``BrowserSessionMiddleware`` on first sight of a valid
    ``sess_…`` browser-session ID.  Works for anonymous and authenticated users.
    """

    session_id = models.CharField(max_length=64, unique=True, db_index=True)
    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="browser_sessions",
    )
    user_id_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="SHA-256 of user.pk — no PII stored.",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField()
    release = models.CharField(max_length=64, blank=True, default="")
    browser = models.CharField(max_length=100, blank=True, default="")
    device = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text="desktop | mobile | tablet",
    )
    replay_id = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Populated in Phase 4 when a replay SDK is configured.",
    )
    entry_path = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(fields=["account", "-last_seen_at"]),
            models.Index(fields=["-last_seen_at"]),
        ]

    def __str__(self):
        return self.session_id


class UxEvent(models.Model):
    """One UX friction or JS error event emitted by the browser tracker.

    Linked to a ``BrowserSession`` via session FK.  The ``request_id`` field
    holds the nearest backend request ID known to the frontend at the time of
    the event, enabling rage-click → 500 correlation.
    """

    class Type(models.TextChoices):
        RAGE_CLICK = "rage_click", "Rage click"
        DEAD_CLICK = "dead_click", "Dead click"
        RAPID_NAV = "rapid_navigation", "Rapid navigation"
        FORM_FAILURE = "repeated_form_failure", "Repeated form failure"
        JS_ERROR = "js_error", "JS error"

    session = models.ForeignKey(
        BrowserSession,
        on_delete=models.CASCADE,
        related_name="ux_events",
    )
    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ux_events",
    )
    type = models.CharField(max_length=40, choices=Type.choices)
    page = models.CharField(max_length=255, blank=True, default="")
    target = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Sanitised CSS selector; no attribute values containing user data.",
    )
    click_count = models.PositiveSmallIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Bounded at 16 KB in the ingest view.",
    )
    occurred_at = models.DateTimeField(auto_now_add=True)
    request_id = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="Nearest backend request ID known to the frontend at event time.",
    )
    sentry_event_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(fields=["session", "-occurred_at"]),
            models.Index(fields=["account", "-occurred_at"]),
            models.Index(fields=["type", "-occurred_at"]),
        ]

    def __str__(self):
        return f"{self.type} on {self.page}"


class MessageStatsDaily(models.Model):
    """Per-day rollup of message outcomes, incremented from MessageEvent.

    Dimensions are nullable; a NULL dimension means "all". The single fully
    dimensioned row per ``(account, domain, template, campaign, day, key_mode)``
    is maintained incrementally and reconciled nightly from raw events.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="message_stats"
    )
    domain = models.ForeignKey(
        "email_service.EmailDomain", on_delete=models.CASCADE, blank=True, null=True
    )
    template = models.ForeignKey(
        "email_service.EmailTemplate", on_delete=models.SET_NULL, blank=True, null=True
    )
    campaign = models.ForeignKey(
        "email_service.BulkEmailCampaign",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    day = models.DateField()
    key_mode = models.CharField(max_length=10, default="live")

    sent = models.PositiveIntegerField(default=0)
    delivered = models.PositiveIntegerField(default=0)
    bounced = models.PositiveIntegerField(default=0)
    complained = models.PositiveIntegerField(default=0)
    rejected = models.PositiveIntegerField(default=0)
    opened = models.PositiveIntegerField(default=0)
    clicked = models.PositiveIntegerField(default=0)
    unique_opens = models.PositiveIntegerField(default=0)
    unique_clicks = models.PositiveIntegerField(default=0)
    unsubscribed = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "domain", "template", "campaign", "day", "key_mode"],
                name="uniq_message_stats_dims",
            )
        ]
        indexes = [models.Index(fields=["account", "day"])]

    def __str__(self):
        return f"stats {self.account_id} {self.day}"
