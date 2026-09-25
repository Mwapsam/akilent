"""AI's own records: a business's opt-in, and the proposals AI makes.

Nothing here is read by the engine or the inbox to decide anything. Every row can be deleted and
Akilent keeps working exactly as before, which is the point: no domain model gains an AI column.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class AISettings(models.Model):
    """One business's AI switch and the facts AI may use. Off until the owner opts in."""

    account = models.OneToOneField("accounts.Account", on_delete=models.CASCADE, related_name="ai_settings")
    enabled = models.BooleanField(default=False)
    # Prices, address, hours, policies: the only facts AI may state about the business.
    business_notes = models.TextField(blank=True, default="")
    consented_at = models.DateTimeField(null=True, blank=True)
    consented_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AI settings for account {self.account_id} ({'on' if self.enabled else 'off'})"


class AIProposal(models.Model):
    """Something AI proposes doing in a conversation. A person decides; AI never acts.

    ``version``/``action``/``confidence``/``reason``/``payload`` are the proposal contract (see
    ``apps.ai.proposals``). The outcome fields exist from day one so acceptance and edit rates can
    be measured without a later migration.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"      # queued, the model hasn't answered yet
        READY = "ready", "Ready"            # a valid proposal is waiting for a person
        ERROR = "error", "Error"            # the model failed or proposed something invalid
        USED = "used", "Used"               # a person sent it (possibly edited)
        DISMISSED = "dismissed", "Dismissed"
        EXPIRED = "expired", "Expired"      # overtaken by a newer message or a human reply

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="ai_proposals")
    conversation = models.ForeignKey(
        "conversations.Conversation", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    trigger_message = models.ForeignKey(
        "conversations.Message", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    version = models.PositiveSmallIntegerField(default=1)
    action = models.CharField(max_length=32, blank=True, default="")
    confidence = models.FloatField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    provider = models.CharField(max_length=40, blank=True, default="")
    model = models.CharField(max_length=80, blank=True, default="")
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    edited_before_send = models.BooleanField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["account", "status", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "trigger_message"],
                condition=models.Q(trigger_message__isnull=False),
                name="one_ai_proposal_per_message",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"AI proposal {self.pk} ({self.action or 'pending'}, {self.status})"
