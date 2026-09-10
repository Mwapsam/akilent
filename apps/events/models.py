"""Public business-event ingestion (Phase 5).

`POST /v1/events` lets a developer send domain facts like ``invoice.paid``.
Each is stored here, linked to a Contact, appended to that contact's activity,
and published to the in-process dispatcher so the automation engine (Phase 6)
can react. This decouples "something happened in my product" from "send an
email".
"""
from __future__ import annotations

import secrets

from django.db import models


def _event_public_id() -> str:
    return "bev_" + secrets.token_hex(16)


class BusinessEvent(models.Model):
    public_id = models.CharField(
        max_length=40, unique=True, default=_event_public_id, editable=False
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="business_events"
    )
    name = models.CharField(max_length=128)  # e.g. "invoice.paid"
    contact = models.ForeignKey(
        "contacts.Contact",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="business_events",
    )
    # Raw customer reference as supplied (email or con_ id) — kept even if the
    # contact is later deleted.
    customer_ref = models.CharField(max_length=255, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=40, default="api")

    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        indexes = [
            models.Index(fields=["account", "name", "received_at"]),
            models.Index(fields=["account", "received_at"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.account_id})"
