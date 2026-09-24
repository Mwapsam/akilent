"""Contacts, lists, typed custom attributes, imports, activity, and segments.

Phase 4 of the platform roadmap — the audience/data layer that campaigns,
personalization, workflows, and customer profiles all build on. Before this,
audiences were ephemeral CSV rows on a campaign.
"""
from __future__ import annotations

import secrets

from django.db import models
from django.utils import timezone
from django.utils.text import slugify


def _contact_public_id() -> str:
    return "con_" + secrets.token_hex(16)


class Contact(models.Model):
    class Status(models.TextChoices):
        SUBSCRIBED = "subscribed", "Subscribed"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        BOUNCED = "bounced", "Bounced"
        COMPLAINED = "complained", "Complained"

    class ConsentStatus(models.TextChoices):
        """Provenance of the recipient's permission to be emailed.

        Deliberately separate from ``status``: ``status`` is the *effective*
        deliverability state (what segments and exports filter on), while this
        records *how we know* we may mail them. An address can be SUBSCRIBED
        with UNKNOWN consent — that is exactly the pre-consent backlog we need
        to be able to see and report on.
        """

        UNKNOWN = "unknown", "Unknown"
        OPTED_IN = "opted_in", "Opted in"
        OPTED_OUT = "opted_out", "Opted out"

    public_id = models.CharField(
        max_length=40, unique=True, default=_contact_public_id, editable=False
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="email_contacts"
    )
    # Canonical identity invariant: within an account, every non-null email
    # identifies at most one Contact, every non-null normalized phone
    # identifies at most one Contact, and a Contact may have phone only,
    # email only, or both (see the partial unique constraints below). Channel
    # identities (e.g. WhatsAppContact.contact) resolve to this Contact, and
    # workflows always execute against Contact, never a channel-specific model.
    email = models.EmailField(blank=True, null=True, default=None)
    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    locale = models.CharField(max_length=15, blank=True, default="")
    # E.164-normalized (see apps.whatsapp.models.contact.normalize_phone).
    phone = models.CharField(max_length=20, blank=True, null=True, default=None)

    # Free-form typed attributes; keys are optionally declared in CustomAttributeDef.
    attributes = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SUBSCRIBED
    )
    source = models.CharField(max_length=40, blank=True, default="")

    # --- Email consent (CAN-SPAM / SES: we must be able to show how a
    # recipient came to be on a list). Mirrors the shape already used for
    # WhatsApp in apps.whatsapp.models.contact.WhatsAppContact.
    consent_status = models.CharField(
        max_length=20, choices=ConsentStatus.choices, default=ConsentStatus.UNKNOWN
    )
    consent_source = models.CharField(max_length=100, blank=True, default="")
    consent_at = models.DateTimeField(blank=True, null=True)
    consent_ip = models.GenericIPAddressField(blank=True, null=True)
    # Free-form proof: signup form URL, import id, the attestation text shown
    # to the operator, etc.
    consent_evidence = models.JSONField(default=dict, blank=True)
    opt_out_at = models.DateTimeField(blank=True, null=True)
    opt_out_reason = models.CharField(max_length=255, blank=True, default="")

    first_seen = models.DateTimeField(auto_now_add=True)
    last_engaged_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Owner-facing labels ("pricing-enquiry", "vip"). Managed through apps.contacts.tags.
    tags = models.ManyToManyField("contacts.Tag", blank=True, related_name="contacts")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "email"],
                condition=models.Q(email__isnull=False),
                name="unique_account_contact_email",
            ),
            models.UniqueConstraint(
                fields=["account", "phone"],
                condition=models.Q(phone__isnull=False),
                name="unique_account_contact_phone",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["account", "consent_status"]),
            models.Index(fields=["account", "last_engaged_at"]),
            models.Index(fields=["account", "phone"]),
        ]
        ordering = ["-first_seen"]

    def __str__(self):
        return self.email or self.phone or self.public_id

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)

    @property
    def whatsapp_opted_out(self) -> bool:
        """True if any of this contact's WhatsApp identities has opted out (e.g. STOP).

        WhatsApp-only: ``status`` is email subscription state and is deliberately not
        touched by a WhatsApp opt-out (nor does an email unsubscribe opt out of WhatsApp).
        """
        return self.whatsapp_contacts.filter(opt_in_status="opted_out").exists()

    @property
    def is_opted_out(self) -> bool:
        return self.consent_status == self.ConsentStatus.OPTED_OUT

    def record_opt_in(self, source: str, *, ip: str = "", evidence: dict | None = None) -> None:
        """Record proof that this contact agreed to be emailed.

        Callers own the timeline entry — unlike the WhatsApp equivalent this
        does not emit a ContactEvent, so an unsubscribe flow can write a single
        event covering both the consent change and the status change.
        """
        self.consent_status = self.ConsentStatus.OPTED_IN
        self.consent_source = source[:100]
        self.consent_at = timezone.now()
        self.consent_ip = ip or None
        self.consent_evidence = evidence or {}
        self.opt_out_at = None
        self.opt_out_reason = ""
        self.save(update_fields=[
            "consent_status", "consent_source", "consent_at", "consent_ip",
            "consent_evidence", "opt_out_at", "opt_out_reason", "updated_at",
        ])

    def record_opt_out(self, reason: str) -> None:
        """Withdraw consent, preserving the original opt-in proof.

        ``consent_source``/``consent_at``/``consent_evidence`` are deliberately
        left intact so we can still show how the address got onto the list.
        """
        self.consent_status = self.ConsentStatus.OPTED_OUT
        self.opt_out_at = timezone.now()
        self.opt_out_reason = reason[:255]
        self.save(update_fields=[
            "consent_status", "opt_out_at", "opt_out_reason", "updated_at",
        ])


class Tag(models.Model):
    """A label an owner (or an automation) puts on customers: "pricing-enquiry", "vip".

    ``slug`` is the identity ("VIP" and "vip " are one tag); ``name`` is how it was first
    written and is what people see. Created on demand by ``apps.contacts.tags``, never by
    hand, so there is no tag-management screen to maintain.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="contact_tags"
    )
    name = models.CharField(max_length=40)
    slug = models.SlugField(max_length=60)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "slug"], name="uniq_contacttag_account_slug")
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name


class ContactList(models.Model):
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="contact_lists"
    )
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160)
    contacts = models.ManyToManyField(
        Contact, through="ContactListMembership", related_name="lists"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "slug"], name="uniq_contactlist_account_slug"
            )
        ]
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:160]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ContactListMembership(models.Model):
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE)
    contact_list = models.ForeignKey(ContactList, on_delete=models.CASCADE)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["contact", "contact_list"], name="uniq_contactlist_membership"
            )
        ]


_SAMPLE_VALUE_BY_ATTRIBUTE_TYPE = {
    "string": "Sample text", "number": "123", "boolean": "Yes", "date": "2026-09-26",
}


class CustomAttributeDef(models.Model):
    class Type(models.TextChoices):
        STRING = "string", "Text"
        NUMBER = "number", "Number"
        BOOLEAN = "boolean", "Yes / no"
        DATE = "date", "Date"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="contact_attributes"
    )
    key = models.CharField(max_length=64)
    type = models.CharField(max_length=10, choices=Type.choices, default=Type.STRING)
    label = models.CharField(max_length=150, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "key"], name="uniq_custom_attr_account_key"
            )
        ]
        ordering = ["key"]

    def __str__(self):
        return f"{self.key} ({self.type})"

    @property
    def sample_value(self) -> str:
        """A plausible display value for pickers/previews that have no real
        Contact to read from yet (e.g. apps.whatsapp's template builder)."""
        return _SAMPLE_VALUE_BY_ATTRIBUTE_TYPE[self.type]


class ContactImport(models.Model):
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="contact_imports"
    )
    filename = models.CharField(max_length=255, blank=True, default="")
    row_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    mapping = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"import {self.filename or self.pk} ({self.row_count} rows)"


class ContactEvent(models.Model):
    """Append-only activity stream for a contact (email events + business events)."""

    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="events"
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="contact_events"
    )
    type = models.CharField(max_length=64)
    occurred_at = models.DateTimeField()
    data = models.JSONField(default=dict, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [
            models.Index(fields=["contact", "occurred_at"]),
            models.Index(fields=["account", "type", "occurred_at"]),
        ]

    def __str__(self):
        return f"{self.type} @ {self.occurred_at:%Y-%m-%d}"


class Segment(models.Model):
    """A saved boolean query over contact attributes + rollup facts.

    ``definition`` is a small AST — see ``apps.contacts.segments`` for the shape
    and the ORM compiler.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="segments"
    )
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160)
    definition = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "slug"], name="uniq_segment_account_slug"
            )
        ]
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:160]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
