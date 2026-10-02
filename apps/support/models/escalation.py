from django.db import models


class SupportEscalation(models.Model):
    """Records a single escalation step for a ticket."""

    MANUAL = "manual"
    SLA_BREACH = "sla_breach"
    TIER_RULE = "tier_rule"
    AMOUNT_RULE = "amount_rule"
    SECURITY = "security"

    REASON_CHOICES = [
        (MANUAL, "Manual escalation"),
        (SLA_BREACH, "SLA breach"),
        (TIER_RULE, "Customer tier rule"),
        (AMOUNT_RULE, "Amount threshold rule"),
        (SECURITY, "Security incident"),
    ]

    ticket = models.ForeignKey(
        "support.SupportTicket", on_delete=models.CASCADE, related_name="escalations"
    )
    from_level = models.CharField(max_length=2)
    to_level = models.CharField(max_length=2)
    reason = models.CharField(max_length=20, choices=REASON_CHOICES, default=MANUAL)
    notes = models.TextField(blank=True, default="")
    escalated_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_escalations_made",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return (
            f"[{self.ticket.ticket_number}] "
            f"{self.from_level} → {self.to_level} ({self.get_reason_display()})"
        )


class SupportAssignment(models.Model):
    """Audit trail of every agent assignment change on a ticket."""

    ticket = models.ForeignKey(
        "support.SupportTicket", on_delete=models.CASCADE, related_name="assignments"
    )
    from_agent = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_assignments_from",
    )
    to_agent = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_assignments_to",
    )
    assigned_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_assignments_made",
    )
    notes = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"[{self.ticket.ticket_number}] assignment #{self.pk}"
