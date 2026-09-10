"""Contacts, lists, typed custom attributes, imports, activity, and segments.

Phase 4 of the platform roadmap — the audience/data layer that campaigns,
personalization, workflows, and customer profiles all build on. Before this,
audiences were ephemeral CSV rows on a campaign.
"""
from __future__ import annotations

import secrets

from django.db import models
from django.utils.text import slugify


def _contact_public_id() -> str:
    return "con_" + secrets.token_hex(16)


class Contact(models.Model):
    class Status(models.TextChoices):
        SUBSCRIBED = "subscribed", "Subscribed"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"
        BOUNCED = "bounced", "Bounced"
        COMPLAINED = "complained", "Complained"

    public_id = models.CharField(
        max_length=40, unique=True, default=_contact_public_id, editable=False
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="email_contacts"
    )
    email = models.EmailField()
    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    locale = models.CharField(max_length=15, blank=True, default="")

    # Free-form typed attributes; keys are optionally declared in CustomAttributeDef.
    attributes = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SUBSCRIBED
    )
    source = models.CharField(max_length=40, blank=True, default="")

    first_seen = models.DateTimeField(auto_now_add=True)
    last_engaged_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "email"], name="uniq_contact_account_email"
            )
        ]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["account", "last_engaged_at"]),
        ]
        ordering = ["-first_seen"]

    def __str__(self):
        return self.email

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)


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
