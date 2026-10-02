"""Core support ticket model and its generic reference table."""

from __future__ import annotations

import secrets
from datetime import timedelta

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

from .sla import SLAPolicy


def _ticket_number() -> str:
    return "SUP-" + secrets.token_hex(4).upper()


class SupportTicket(models.Model):
    # ------------------------------------------------------------------ #
    # Status lifecycle
    # ------------------------------------------------------------------ #
    NEW = "new"
    TRIAGED = "triaged"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    WAITING_CUSTOMER = "waiting_customer"
    WAITING_INTERNAL = "waiting_internal"
    ESCALATED = "escalated"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REOPENED = "reopened"

    STATUS_CHOICES = [
        (NEW, "New"),
        (TRIAGED, "Triaged"),
        (ASSIGNED, "Assigned"),
        (IN_PROGRESS, "In Progress"),
        (WAITING_CUSTOMER, "Waiting on Customer"),
        (WAITING_INTERNAL, "Waiting Internal"),
        (ESCALATED, "Escalated"),
        (RESOLVED, "Resolved"),
        (CLOSED, "Closed"),
        (REOPENED, "Reopened"),
    ]

    # Support levels (who handles the ticket)
    L1 = "l1"
    L2 = "l2"
    L3 = "l3"
    L4 = "l4"

    SUPPORT_LEVEL_CHOICES = [
        (L1, "L1 — Customer Support"),
        (L2, "L2 — Product / Technical"),
        (L3, "L3 — Engineering / Specialist"),
        (L4, "L4 — External / Infrastructure"),
    ]

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    ticket_number = models.CharField(
        max_length=20, unique=True, default=_ticket_number, editable=False
    )

    # ------------------------------------------------------------------ #
    # Ownership
    # ------------------------------------------------------------------ #
    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="support_tickets",
    )
    # The user who opened the ticket (may be null if submitted via API/email).
    submitted_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_support_tickets",
    )

    # ------------------------------------------------------------------ #
    # Classification
    # ------------------------------------------------------------------ #
    category = models.ForeignKey(
        "support.SupportCategory",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tickets",
    )
    queue = models.ForeignKey(
        "support.SupportQueue",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tickets",
    )

    # ------------------------------------------------------------------ #
    # Priority
    # ------------------------------------------------------------------ #
    customer_tier = models.PositiveSmallIntegerField(
        choices=SLAPolicy.TIER_CHOICES, default=SLAPolicy.TIER_1
    )
    priority = models.CharField(
        max_length=2, choices=SLAPolicy.PRIORITY_CHOICES, default=SLAPolicy.P3
    )
    # Severity is a free-text label for the internal notes field; the
    # computed priority is what drives routing and SLA.
    severity_note = models.CharField(max_length=200, blank=True, default="")

    # ------------------------------------------------------------------ #
    # Assignment
    # ------------------------------------------------------------------ #
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=NEW)
    support_level = models.CharField(
        max_length=2, choices=SUPPORT_LEVEL_CHOICES, default=L1
    )
    assigned_agent = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_support_tickets",
    )

    # ------------------------------------------------------------------ #
    # Content
    # ------------------------------------------------------------------ #
    subject = models.CharField(max_length=255)
    description = models.TextField()

    # ------------------------------------------------------------------ #
    # SLA tracking
    # ------------------------------------------------------------------ #
    sla_policy = models.ForeignKey(
        "support.SLAPolicy",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tickets",
    )
    sla_due_at = models.DateTimeField(null=True, blank=True)
    sla_breached = models.BooleanField(default=False)
    first_response_at = models.DateTimeField(null=True, blank=True)

    # ------------------------------------------------------------------ #
    # Timestamps
    # ------------------------------------------------------------------ #
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["priority", "status"]),
            models.Index(fields=["sla_due_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.ticket_number}: {self.subject}"

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def mark_resolved(self) -> None:
        self.status = self.RESOLVED
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolved_at", "updated_at"])

    def mark_closed(self) -> None:
        self.status = self.CLOSED
        self.closed_at = timezone.now()
        self.save(update_fields=["status", "closed_at", "updated_at"])

    def reopen(self) -> None:
        self.status = self.REOPENED
        self.resolved_at = None
        self.closed_at = None
        self.save(update_fields=["status", "resolved_at", "closed_at", "updated_at"])

    def apply_sla(self) -> None:
        """Compute and store sla_due_at from the matching SLAPolicy."""
        try:
            policy = SLAPolicy.objects.get(
                customer_tier=self.customer_tier,
                priority=self.priority,
                is_active=True,
            )
        except SLAPolicy.DoesNotExist:
            return
        self.sla_policy = policy
        self.sla_due_at = timezone.now() + timedelta(
            minutes=policy.first_response_minutes
        )
        self.save(update_fields=["sla_policy", "sla_due_at", "updated_at"])


class SupportTicketReference(models.Model):
    """Generic FK linking a ticket to any related domain object.

    Examples: a payment, an order, a WhatsApp conversation, a contact.
    """

    RELATIONSHIP_CHOICES = [
        ("related", "Related"),
        ("caused_by", "Caused by"),
        ("affects", "Affects"),
    ]

    ticket = models.ForeignKey(
        SupportTicket, on_delete=models.CASCADE, related_name="references"
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.CharField(max_length=255)
    content_object = GenericForeignKey("content_type", "object_id")
    relationship = models.CharField(
        max_length=20, choices=RELATIONSHIP_CHOICES, default="related"
    )
    label = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Ticket Reference"
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.ticket.ticket_number} → {self.content_type} #{self.object_id}"
