"""Insight and RecommendationLog models.

An Insight is a business observation Akilent has made about an account:
a pattern in data that deserves the owner's attention.  It always carries
evidence (structured data backing the claim) and a suggested_action, so the
owner understands why it was raised and what to do about it.

RecommendationLog tracks whether the owner acted on a recommendation and,
eventually, what outcome followed.  This is the explainability layer: every
recommendation Akilent makes must be traceable to evidence and measurable
against an outcome.
"""

from __future__ import annotations

from django.db import models


class Insight(models.Model):
    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        OPPORTUNITY = "opportunity", "Opportunity"
        URGENT = "urgent", "Urgent"

    class Status(models.TextChoices):
        NEW = "new", "New"
        ACKNOWLEDGED = "acknowledged", "Acknowledged"
        ACTED_ON = "acted_on", "Acted on"
        DISMISSED = "dismissed", "Dismissed"
        RESOLVED = "resolved", "Resolved"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="insights"
    )
    # Stable identifier for the rule that produced this insight.  Used for
    # upsert logic: re-running a rule replaces the existing open insight rather
    # than creating duplicates.
    type = models.CharField(max_length=64)
    severity = models.CharField(
        max_length=12, choices=Severity.choices, default=Severity.INFO
    )
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    # Structured data backing the claim.  Shape varies by type; the rule that
    # produced the insight owns the schema.
    evidence = models.JSONField(default=dict, blank=True)
    evidence_count = models.PositiveIntegerField(default=0)
    # A structured suggestion; the rule that produced it owns the schema.
    # E.g. {"action": "campaign", "segment": "inactive_customers", "label": "..."}
    suggested_action = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.NEW)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["account", "type", "status"]),
            models.Index(fields=["account", "severity", "status"]),
        ]
        constraints = [
            # Only one open insight of each type per account at a time.
            # Re-running a rule should update the existing row, not create a duplicate.
            models.UniqueConstraint(
                fields=["account", "type"],
                condition=models.Q(status__in=["new", "acknowledged"]),
                name="uniq_open_insight_per_type",
            )
        ]

    def __str__(self):
        return f"[{self.severity}] {self.title}"


class RecommendationLog(models.Model):
    """Tracks whether an Insight's suggestion was acted on and what outcome followed.

    This is the authority layer: recommendations must earn trust through evidence.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="recommendation_logs"
    )
    insight = models.ForeignKey(
        Insight,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendation_logs",
    )
    recommended_at = models.DateTimeField(auto_now_add=True)
    accepted = models.BooleanField(null=True, blank=True)
    acted_at = models.DateTimeField(blank=True, null=True)
    outcome_measured_at = models.DateTimeField(blank=True, null=True)
    # Free-form outcome: {"metric": "response_time", "before": 45, "after": 12, "unit": "min"}
    outcome_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-recommended_at"]
        indexes = [
            models.Index(fields=["account", "recommended_at"]),
        ]

    def __str__(self):
        state = (
            "accepted"
            if self.accepted
            else ("declined" if self.accepted is False else "pending")
        )
        return f"Recommendation {self.pk} ({state})"
