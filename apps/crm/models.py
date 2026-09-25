"""Phase 2 thin CRM: Lead -> Pipeline -> Deal -> Stage.

Deliberately minimal per the plan ("keep CRM extremely simple... you don't
need dozens of CRM entities initially"): no accounts/companies, no custom
fields, no multi-currency conversion. Every object carries an explicit
``account`` FK (Locked Implementation Decision #1) and links to the
canonical ``apps.contacts.Contact`` — never a channel-specific identity.

``apps.whatsapp.models.contact.CrmBinding`` (the pre-Phase-2 placeholder
that referenced external lead/deal IDs by string) is left untouched here —
migrating its call sites is a separate, deliberately deferred effort so this
phase doesn't risk destabilizing the existing WhatsApp implementation.
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models


def _lead_public_id() -> str:
    return "lead_" + secrets.token_hex(12)


def _deal_public_id() -> str:
    return "deal_" + secrets.token_hex(12)


class Pipeline(models.Model):
    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="pipelines")
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "slug"], name="unique_pipeline_slug_per_account"),
            models.UniqueConstraint(
                fields=["account"], condition=models.Q(is_default=True),
                name="one_default_pipeline_per_account",
            ),
        ]

    def __str__(self):
        return self.name

    @classmethod
    def ensure_default(cls, account) -> "Pipeline":
        """Get or create the account's default pipeline with a starter stage set.

        Keeps the thin-CRM UI usable out of the box — a business doesn't have
        to configure a pipeline before its first Lead/Deal can exist.
        """
        pipeline = cls.objects.filter(account=account, is_default=True).first()
        if pipeline is not None:
            return pipeline
        pipeline = cls.objects.create(account=account, name="Sales", slug="sales", is_default=True)
        starter_stages = [
            ("New", False, False),
            ("Contacted", False, False),
            ("Won", True, False),
            ("Lost", False, True),
        ]
        for order, (name, is_won, is_lost) in enumerate(starter_stages):
            Stage.objects.create(
                pipeline=pipeline, name=name, order=order, is_won=is_won, is_lost=is_lost,
            )
        return pipeline


class Stage(models.Model):
    pipeline = models.ForeignKey(Pipeline, on_delete=models.CASCADE, related_name="stages")
    name = models.CharField(max_length=100)
    order = models.PositiveSmallIntegerField(default=0)
    is_won = models.BooleanField(default=False)
    is_lost = models.BooleanField(default=False)

    class Meta:
        ordering = ["order"]
        constraints = [
            models.UniqueConstraint(fields=["pipeline", "order"], name="unique_stage_order_per_pipeline"),
        ]

    def __str__(self):
        return f"{self.pipeline.name} / {self.name}"


class Lead(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "New"
        CONTACTED = "contacted", "Contacted"
        QUALIFIED = "qualified", "Qualified"
        CONVERTED = "converted", "Converted"
        LOST = "lost", "Lost"

    public_id = models.CharField(max_length=40, unique=True, default=_lead_public_id, editable=False)
    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="leads")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="leads")

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NEW)
    source = models.CharField(max_length=50, blank=True, default="")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="owned_leads"
    )

    # The conversation this came from, fixed when it was created so a later reply never
    # rewrites history. Null when nothing in the inbox led here (walk-in, manual entry).
    conversation = models.ForeignKey(
        "conversations.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="leads"
    )

    converted_at = models.DateTimeField(blank=True, null=True)
    converted_to_deal = models.ForeignKey(
        "crm.Deal", on_delete=models.SET_NULL, null=True, blank=True, related_name="originating_leads"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["account", "owner"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"Lead: {self.contact} ({self.get_status_display()})"


class Deal(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        WON = "won", "Won"
        LOST = "lost", "Lost"

    public_id = models.CharField(max_length=40, unique=True, default=_deal_public_id, editable=False)
    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="deals")
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE, related_name="deals")
    pipeline = models.ForeignKey(Pipeline, on_delete=models.PROTECT, related_name="deals")
    stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name="deals")

    title = models.CharField(max_length=200)
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=8, blank=True, default="")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="owned_deals"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    # The conversation this came from, fixed when it was created so a later reply never
    # rewrites history. Null when nothing in the inbox led here (walk-in, manual entry).
    conversation = models.ForeignKey(
        "conversations.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="deals"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["account", "pipeline", "stage"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.title} ({self.get_status_display()})"
