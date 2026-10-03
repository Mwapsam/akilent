from django.db import models


class SupportEvent(models.Model):
    """Immutable audit-log entry for every state change on a ticket.

    These power the ticket timeline view and SLA history.
    """

    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    PRIORITY_CHANGED = "priority_changed"
    ASSIGNED = "assigned"
    ESCALATED = "escalated"
    SLA_BREACHED = "sla_breached"
    FIRST_RESPONSE = "first_response"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REOPENED = "reopened"
    NOTE_ADDED = "note_added"
    REFERENCE_ADDED = "reference_added"
    CREATED_FROM_CONVERSATION = "created_from_conversation"

    EVENT_CHOICES = [
        (CREATED, "Ticket created"),
        (CREATED_FROM_CONVERSATION, "Created from conversation"),
        (STATUS_CHANGED, "Status changed"),
        (PRIORITY_CHANGED, "Priority changed"),
        (ASSIGNED, "Assigned to agent"),
        (ESCALATED, "Escalated"),
        (SLA_BREACHED, "SLA breached"),
        (FIRST_RESPONSE, "First response sent"),
        (RESOLVED, "Resolved"),
        (CLOSED, "Closed"),
        (REOPENED, "Reopened"),
        (NOTE_ADDED, "Internal note added"),
        (REFERENCE_ADDED, "Reference added"),
    ]

    ticket = models.ForeignKey(
        "support.SupportTicket", on_delete=models.CASCADE, related_name="events"
    )
    event_type = models.CharField(max_length=30, choices=EVENT_CHOICES)
    actor = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="support_events",
    )
    # JSON-serialisable snapshot of what changed, e.g. {"from": "new", "to": "in_progress"}
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"[{self.ticket.ticket_number}] {self.get_event_type_display()}"
