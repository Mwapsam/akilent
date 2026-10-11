"""HubSpot connector models.

Supports bidirectional sync between Akilent Contacts/Leads/Deals and
HubSpot Contacts/Deals via OAuth, outbound jobs, and inbound webhooks.
"""

from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.accounts.fields import EncryptedTextField


def _state_token() -> str:
    return secrets.token_urlsafe(32)


class HubSpotConnection(models.Model):
    """OAuth credentials linking an Akilent Account to a HubSpot portal."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        DISCONNECTED = "disconnected", "Disconnected"

    account = models.OneToOneField(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="hubspot_connection",
    )
    portal_id = models.CharField(max_length=50, blank=True, default="")
    access_token = EncryptedTextField(blank=True, null=True)
    refresh_token = EncryptedTextField(blank=True, null=True)
    token_expires_at = models.DateTimeField(blank=True, null=True)
    scopes = models.JSONField(default=list)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    # Pending OAuth: single-use state token
    oauth_state = models.CharField(max_length=64, blank=True, default="")
    oauth_state_created_at = models.DateTimeField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["portal_id"])]

    def is_token_fresh(self) -> bool:
        if not self.token_expires_at:
            return bool(self.access_token)
        return timezone.now() < self.token_expires_at

    def __str__(self) -> str:
        return f"{self.account} → HubSpot portal {self.portal_id}"


class HubSpotContactLink(models.Model):
    """Maps an Akilent Contact to a HubSpot Contact (vid/object id)."""

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="hs_contact_links"
    )
    contact = models.OneToOneField(
        "contacts.Contact",
        on_delete=models.CASCADE,
        related_name="hubspot_link",
    )
    hs_object_id = models.CharField(max_length=50)
    remote_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "hs_object_id"],
                name="unique_hs_contact_per_account",
            )
        ]

    def __str__(self) -> str:
        return f"Contact {self.contact_id} ↔ HS {self.hs_object_id}"


class HubSpotDealLink(models.Model):
    """Maps an Akilent Deal to a HubSpot Deal."""

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="hs_deal_links"
    )
    deal = models.OneToOneField(
        "crm.Deal",
        on_delete=models.CASCADE,
        related_name="hubspot_link",
    )
    hs_object_id = models.CharField(max_length=50)
    remote_archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "hs_object_id"],
                name="unique_hs_deal_per_account",
            )
        ]

    def __str__(self) -> str:
        return f"Deal {self.deal_id} ↔ HS {self.hs_object_id}"


class HubSpotFieldState(models.Model):
    """Per-field sync state for a linked object."""

    class Direction(models.TextChoices):
        AKILENT = "akilent", "Akilent (push only)"
        HUBSPOT = "hubspot", "HubSpot (pull only)"
        BIDIRECTIONAL = "bidirectional", "Bidirectional"

    # Exactly one of contact_link / deal_link must be set.
    contact_link = models.ForeignKey(
        HubSpotContactLink,
        on_delete=models.CASCADE,
        related_name="field_states",
        null=True,
        blank=True,
    )
    deal_link = models.ForeignKey(
        HubSpotDealLink,
        on_delete=models.CASCADE,
        related_name="field_states",
        null=True,
        blank=True,
    )
    field = models.CharField(max_length=100)
    synced_value_hash = models.CharField(max_length=64, blank=True, default="")
    synced_source_ts = models.DateTimeField(blank=True, null=True)
    synced_at = models.DateTimeField(blank=True, null=True)
    direction = models.CharField(
        max_length=20, choices=Direction.choices, default=Direction.BIDIRECTIONAL
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(contact_link__isnull=False, deal_link__isnull=True)
                    | models.Q(contact_link__isnull=True, deal_link__isnull=False)
                ),
                name="hs_field_state_exactly_one_link",
            ),
            models.UniqueConstraint(
                fields=["contact_link", "field"],
                condition=models.Q(contact_link__isnull=False),
                name="unique_hs_field_state_contact",
            ),
            models.UniqueConstraint(
                fields=["deal_link", "field"],
                condition=models.Q(deal_link__isnull=False),
                name="unique_hs_field_state_deal",
            ),
        ]

    @staticmethod
    def hash_value(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    def __str__(self) -> str:
        link = self.contact_link or self.deal_link
        return f"FieldState({link}, {self.field})"


class HubSpotSyncJob(models.Model):
    """Outbound sync job: push one object's changed fields to HubSpot."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    class ObjectType(models.TextChoices):
        CONTACT = "contact", "Contact"
        DEAL = "deal", "Deal"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="hs_sync_jobs"
    )
    object_type = models.CharField(max_length=10, choices=ObjectType.choices)
    contact_link = models.ForeignKey(
        HubSpotContactLink,
        on_delete=models.CASCADE,
        related_name="sync_jobs",
        null=True,
        blank=True,
    )
    deal_link = models.ForeignKey(
        HubSpotDealLink,
        on_delete=models.CASCADE,
        related_name="sync_jobs",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    sent_payload = models.JSONField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    retry_after = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "retry_after"]),
        ]

    def __str__(self) -> str:
        return f"SyncJob({self.object_type}, {self.status})"


class HubSpotWebhookEvent(models.Model):
    """Deduplicated inbound webhook event from HubSpot."""

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="hs_webhook_events",
    )
    event_id = models.CharField(max_length=100, unique=True)
    portal_id = models.CharField(max_length=50)
    object_type = models.CharField(max_length=20)
    object_id = models.CharField(max_length=50)
    property_name = models.CharField(max_length=100)
    property_value = models.TextField(blank=True, default="")
    # HubSpot's occurredAt — the value-change time, not delivery time.
    occurred_at = models.DateTimeField()
    raw_payload = models.JSONField()
    processed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["portal_id", "object_id"]),
            models.Index(fields=["processed", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"WHEvent({self.event_id}, {self.object_type}/{self.object_id})"


class SyncConflict(models.Model):
    """A field value that conflicted during bidirectional sync."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED_LOCAL = "resolved_local", "Resolved (kept Akilent)"
        RESOLVED_REMOTE = "resolved_remote", "Resolved (kept HubSpot)"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="hs_conflicts"
    )
    contact_link = models.ForeignKey(
        HubSpotContactLink,
        on_delete=models.CASCADE,
        related_name="conflicts",
        null=True,
        blank=True,
    )
    deal_link = models.ForeignKey(
        HubSpotDealLink,
        on_delete=models.CASCADE,
        related_name="conflicts",
        null=True,
        blank=True,
    )
    field = models.CharField(max_length=100)
    local_value = models.TextField(blank=True, default="")
    remote_value = models.TextField(blank=True, default="")
    local_source_ts = models.DateTimeField(blank=True, null=True)
    remote_source_ts = models.DateTimeField(blank=True, null=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN
    )
    resolver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_hs_conflicts",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self) -> str:
        return f"SyncConflict({self.field}, {self.status})"


class ReconcileCursor(models.Model):
    """Tracks pagination state for a HubSpot reconciliation run."""

    class ObjectType(models.TextChoices):
        CONTACT = "contact", "Contact"
        DEAL = "deal", "Deal"
        ARCHIVED_CONTACT = "archived_contact", "Archived Contact"
        ARCHIVED_DEAL = "archived_deal", "Archived Deal"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="hs_cursors"
    )
    object_type = models.CharField(max_length=30, choices=ObjectType.choices)
    last_modified_ts = models.DateTimeField(blank=True, null=True)
    last_object_id = models.CharField(max_length=50, blank=True, default="")
    after_cursor = models.CharField(max_length=200, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "object_type"],
                name="unique_hs_cursor_per_account_type",
            )
        ]

    def __str__(self) -> str:
        return f"Cursor({self.account_id}, {self.object_type}, {self.last_modified_ts})"
