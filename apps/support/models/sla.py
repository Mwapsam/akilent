from django.db import models


class SLAPolicy(models.Model):
    """Configurable SLA rule keyed by customer tier + priority.

    All time values are in minutes. Business-hours enforcement is left
    for a later phase — for now all times are wall-clock.
    """

    # Customer tiers (mirrors the design spec)
    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3
    TIER_4 = 4

    TIER_CHOICES = [
        (TIER_1, "Tier 1 — Standard"),
        (TIER_2, "Tier 2 — Growth"),
        (TIER_3, "Tier 3 — Business"),
        (TIER_4, "Tier 4 — Enterprise / Strategic"),
    ]

    # Issue priorities
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"
    P4 = "p4"

    PRIORITY_CHOICES = [
        (P1, "P1 — Critical"),
        (P2, "P2 — High"),
        (P3, "P3 — Normal"),
        (P4, "P4 — Low"),
    ]

    customer_tier = models.PositiveSmallIntegerField(choices=TIER_CHOICES)
    priority = models.CharField(max_length=2, choices=PRIORITY_CHOICES)

    # Minutes until first public response is required.
    first_response_minutes = models.PositiveIntegerField()
    # Minutes between mandatory status updates while in-progress.
    update_frequency_minutes = models.PositiveIntegerField(null=True, blank=True)
    # Target minutes to full resolution.
    resolution_minutes = models.PositiveIntegerField()
    # Minutes of no progress before auto-escalation fires.
    escalation_after_minutes = models.PositiveIntegerField(null=True, blank=True)

    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "SLA Policy"
        verbose_name_plural = "SLA Policies"
        unique_together = [("customer_tier", "priority")]
        ordering = ["customer_tier", "priority"]

    def __str__(self) -> str:
        return (
            f"Tier {self.customer_tier} / {self.get_priority_display()} — "
            f"{self.first_response_minutes}m first response"
        )
