"""Akilent's own product-marketing email: who agreed to hear from us, lists, campaigns.

This is not the tenant email product (apps.email). Nothing here belongs to an Account, and
sending goes through the system mail provider, so it works while the tenant Email channel is
switched off. A marketing unsubscribe never stops system mail (password resets, billing).
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models


def _new_token() -> str:
    return secrets.token_urlsafe(32)


class MarketingSettings(models.Model):
    """One row: the sender details every marketing email needs."""

    postal_address = models.TextField(
        blank=True,
        default="",
        help_text="Printed in every marketing email footer (CAN-SPAM). Sending is blocked until set.",
    )
    from_name = models.CharField(max_length=100, blank=True, default="Akilent Updates")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls) -> MarketingSettings:
        row, _ = cls.objects.get_or_create(pk=1)
        return row


class MarketingSubscriber(models.Model):
    """One address and its consent. The single source of truth for marketing permission."""

    class Status(models.TextChoices):
        SUBSCRIBED = "subscribed", "Subscribed"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        SUPPRESSED = "suppressed", "Bounced or complained"

    class Source(models.TextChoices):
        SIGNUP = "signup", "Signup checkbox"
        PROFILE = "profile", "Profile settings"
        EXISTING_CUSTOMER = "existing_customer", "Existing customer (soft opt-in)"
        IMPORT = "import", "CSV import"
        ADMIN = "admin", "Added by an operator"

    email = models.EmailField(unique=True)
    name = models.CharField(max_length=200, blank=True, default="")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="marketing_subscriptions",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.SUBSCRIBED, db_index=True
    )
    source = models.CharField(max_length=20, choices=Source.choices)
    # Where and when consent was given: form, import file, consent date from the CRM...
    consent_evidence = models.JSONField(default=dict, blank=True)
    consented_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    token = models.CharField(max_length=64, unique=True, default=_new_token)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.email

    @property
    def is_subscribed(self) -> bool:
        return self.status == self.Status.SUBSCRIBED


class MarketingList(models.Model):
    """A static list (members added by hand or CSV) or a dynamic one (a saved filter over
    subscribed Akilent users, resolved when a campaign sends)."""

    class Kind(models.TextChoices):
        STATIC = "static", "Static (imported or added)"
        DYNAMIC = "dynamic", "Dynamic (Akilent users matching a filter)"

    name = models.CharField(max_length=150)
    description = models.CharField(max_length=255, blank=True, default="")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.STATIC)
    # Dynamic only: {"role": "owners"|"all", "plans": [slug], "statuses": [status],
    # "joined_after": "YYYY-MM-DD", "joined_before": "YYYY-MM-DD"}
    filters = models.JSONField(default=dict, blank=True)
    members = models.ManyToManyField(
        MarketingSubscriber, through="MarketingListMember", related_name="lists"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class MarketingListMember(models.Model):
    list = models.ForeignKey(MarketingList, on_delete=models.CASCADE)
    subscriber = models.ForeignKey(MarketingSubscriber, on_delete=models.CASCADE)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["list", "subscriber"], name="uniq_marketing_list_member"
            )
        ]


class MarketingCampaign(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SCHEDULED = "scheduled", "Scheduled"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"
        CANCELLED = "cancelled", "Cancelled"

    name = models.CharField(
        max_length=150, help_text="Internal name; recipients don't see it."
    )
    subject = models.CharField(max_length=200)
    preheader = models.CharField(max_length=200, blank=True, default="")
    heading = models.CharField(max_length=200, blank=True, default="")
    # Markdown. Raw HTML is escaped, so what an operator types is what is shown.
    body = models.TextField()
    cta_label = models.CharField(max_length=60, blank=True, default="")
    cta_url = models.URLField(blank=True, default="")
    lists = models.ManyToManyField(MarketingList, related_name="campaigns", blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    scheduled_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Only one send chain may run a campaign at a time: a run holds the lease (token +
    # expiry) and hands the token to the run it queues. A chain that dies lets the lease
    # lapse, so the backstop can take over without ever doubling the send rate.
    lease_token = models.CharField(max_length=64, blank=True, default="")
    lease_until = models.DateTimeField(null=True, blank=True)
    # The last provider error, shown to operators while sends are being retried.
    last_error = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name

    @property
    def is_editable(self) -> bool:
        return self.status in (self.Status.DRAFT, self.Status.SCHEDULED)


class MarketingSend(models.Model):
    """One campaign to one subscriber. Unique, so a retried task never emails anyone twice."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    campaign = models.ForeignKey(
        MarketingCampaign, on_delete=models.CASCADE, related_name="sends"
    )
    subscriber = models.ForeignKey(
        MarketingSubscriber, on_delete=models.CASCADE, related_name="sends"
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    error = models.CharField(max_length=255, blank=True, default="")
    # Provider errors so far; the send stays pending (retried later) until MAX_ATTEMPTS.
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "subscriber"], name="uniq_marketing_send"
            )
        ]
