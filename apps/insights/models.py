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

    ``status`` tracks the full lifecycle so acceptance rate is computable:
        presented → accepted / dismissed / expired
    ``policy_execution`` links to the PolicyExecution that ran as a result of
    acceptance, establishing the explicit audit chain:
        Insight → RecommendationLog → BusinessPolicy → PolicyExecution
    ``outcome_*`` fields are populated later (P1) once business signals exist.
    """

    class Status(models.TextChoices):
        PRESENTED = "presented", "Presented"
        ACCEPTED = "accepted", "Accepted"
        DISMISSED = "dismissed", "Dismissed"
        EXPIRED = "expired", "Expired"

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
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PRESENTED
    )
    # What kind of action was taken when this recommendation was accepted.
    # E.g. "instagram_dm", "whatsapp_message", "manual_outreach".
    action_type = models.CharField(max_length=64, blank=True, default="")
    recommended_at = models.DateTimeField(auto_now_add=True)
    accepted = models.BooleanField(null=True, blank=True)
    acted_at = models.DateTimeField(blank=True, null=True)
    # The conversation this recommendation produced (set when action executes).
    # Gives the forward chain: Insight → RecommendationLog → Conversation.
    conversation = models.ForeignKey(
        "conversations.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="originating_recommendations",
    )
    # Set after the related BusinessPolicy executes — wires the explicit chain.
    # Null until execution happens (may remain null if no policy was created).
    policy_execution = models.ForeignKey(
        "PolicyExecution",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recommendation_logs",
    )
    outcome_measured_at = models.DateTimeField(blank=True, null=True)
    # Progressive outcome signals — updated as business events occur downstream.
    # Schema: {
    #   "dm_sent_at": ISO str | null,
    #   "dm_replied_at": ISO str | null,
    #   "lead_created_at": ISO str | null,
    #   "order_created_at": ISO str | null,
    #   "order_paid_at": ISO str | null,
    #   "revenue": "0.00", "currency": "ZMW"
    # }
    outcome_summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-recommended_at"]
        indexes = [
            models.Index(fields=["account", "recommended_at"]),
            models.Index(fields=["account", "status"]),
        ]

    def __str__(self):
        return f"Recommendation {self.pk} ({self.status})"


class BusinessPolicy(models.Model):
    """An automation rule created (usually from an Insight) that defines
    what Akilent should do when a specific business condition is met.

    This is the bridge from "Akilent observed X" to "whenever X happens, do Y
    automatically."  Policies start as DRAFT (the owner reviews the pre-filled
    proposal from an insight), become ACTIVE when they enable it, and can be
    PAUSED without deletion.

    ``created_from`` links back to the Insight that prompted this policy, giving
    a traceable chain: observation → recommendation → policy → execution.
    """

    class Trigger(models.TextChoices):
        CONVERSATION_UNANSWERED = "conversation_unanswered", "Conversation unanswered"
        LEAD_UNANSWERED = "lead_unanswered", "Lead unanswered"
        CUSTOMER_INACTIVE = "customer_inactive", "Customer inactive"
        REPURCHASE_DUE = "repurchase_due", "Repurchase window elapsed"

    # Maps insight.type to the closest trigger key.
    INSIGHT_TYPE_TO_TRIGGER = {
        "unanswered_conversations": Trigger.CONVERSATION_UNANSWERED,
        "lead_followup_gap": Trigger.LEAD_UNANSWERED,
        "inactive_customers": Trigger.CUSTOMER_INACTIVE,
        "campaign_opportunity": Trigger.REPURCHASE_DUE,
        "instagram_unanswered_intent": Trigger.CONVERSATION_UNANSWERED,
        "instagram_high_engagement_contact": Trigger.CONVERSATION_UNANSWERED,
    }

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="policies"
    )
    name = models.CharField(max_length=200)
    trigger = models.CharField(max_length=64, choices=Trigger.choices)
    # Threshold values that gate the trigger. Shape depends on trigger type.
    # E.g. {"hours": 24} for LEAD_UNANSWERED, {"days": 90} for CUSTOMER_INACTIVE.
    condition = models.JSONField(default=dict, blank=True)
    # What the system should do when the trigger fires.
    # E.g. {"type": "campaign", "label": "Re-order campaign"} or {"type": "notify"}.
    action = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    # The insight that prompted this policy — null when created directly.
    created_from = models.ForeignKey(
        Insight,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="derived_policies",
    )
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["account", "trigger", "status"]),
        ]
        constraints = [
            # Only one policy per source insight per account, regardless of status.
            # Prevents both the race-condition double-create and re-creating a draft
            # after a policy has already been activated or paused from the same insight.
            models.UniqueConstraint(
                fields=["account", "created_from"],
                condition=models.Q(created_from__isnull=False),
                name="uniq_policy_per_insight",
            ),
        ]
        verbose_name_plural = "business policies"

    def __str__(self):
        return f"{self.name} [{self.status}]"


class PolicyExecution(models.Model):
    """Audit record for a single execution attempt of an active BusinessPolicy.

    Each row answers: "When did Akilent evaluate this policy, what did it find,
    and what did it actually do?"

    Field semantics
    ---------------
    trigger_snapshot  — policy config captured at evaluation time (not a live FK)
    target            — who the trigger matched: {"type": "conversation"|"contact",
                         "count": n, "ids": [...up to 50...]}
    result            — what was invoked: {"action": "campaign", "campaign_id": n,
                         "dispatch": "queued"} or {"action": "create_followup",
                         "followup_count": n, "created_count": n, "existing_count": n}
    status            — execution lifecycle (see Status choices)
    error             — populated only on FAILED; describes what went wrong

    This is an execution ledger, not an outcome ledger.  A COMPLETED execution
    means Akilent successfully invoked the configured action — it does not mean
    the customer responded, purchased, or that the recommendation "worked."
    Business outcome measurement is a separate, later concern (P1).
    """

    class Status(models.TextChoices):
        STARTED = "started", "Started"
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    policy = models.ForeignKey(
        BusinessPolicy, on_delete=models.CASCADE, related_name="executions"
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="policy_executions"
    )
    trigger_snapshot = models.JSONField(default=dict, blank=True)
    target = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.STARTED
    )
    error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["policy", "started_at"]),
        ]

    def __str__(self):
        return f"PolicyExecution {self.pk} policy={self.policy_id} [{self.status}]"
