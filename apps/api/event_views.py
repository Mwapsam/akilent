"""Public API for business-event ingestion (Phase 5)."""
from __future__ import annotations

from django.db.models import Count, Max
from django.utils.dateparse import parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

from apps.api.base import BaseApiView
from apps.api.permissions import HasEmailApiFeature
from apps.events.models import BusinessEvent
from apps.events.services import ingest_event


class EventCollectionView(BaseApiView):
    permission_classes = [HasEmailApiFeature]
    idempotency_endpoint = "POST /v1/events"

    @extend_schema(operation_id="events_list", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Events"])
    def get(self, request, *args, **kwargs):
        qs = BusinessEvent.objects.filter(account=request.user).select_related("contact")
        if request.query_params.get("name"):
            qs = qs.filter(name=request.query_params["name"])
        try:
            limit = min(int(request.query_params.get("limit", 50)), 200)
        except (TypeError, ValueError):
            limit = 50
        rows = qs[:limit]
        return Response({"data": [
            {
                "id": e.public_id,
                "event": e.name,
                "customer": e.contact.public_id if e.contact_id else (e.customer_ref or None),
                "data": e.data,
                "occurred_at": e.occurred_at,
                "received_at": e.received_at,
            }
            for e in rows
        ]})

    @extend_schema(operation_id="events_create", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Events"])
    def post(self, request, *args, **kwargs):
        idem = self.begin_idempotency(request)
        d = request.data if isinstance(request.data, dict) else {}
        name = d.get("event")
        if not name:
            return Response(
                {"error": {"code": "validation_error", "message": "`event` is required"}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        occurred_at = parse_datetime(d["occurred_at"]) if d.get("occurred_at") else None
        try:
            ev = ingest_event(
                request.user,
                name=name,
                customer=d.get("customer", ""),
                data=d.get("data") or {},
                occurred_at=occurred_at,
            )
        except ValueError as exc:
            return Response(
                {"error": {"code": "validation_error", "message": str(exc)}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        request.auth.touch()
        return self.finish_idempotency(
            idem,
            Response(
                {
                    "id": ev.public_id,
                    "event": ev.name,
                    "contact": ev.contact.public_id if ev.contact_id else None,
                    "received_at": ev.received_at,
                },
                status=status.HTTP_202_ACCEPTED,
            ),
        )


class EventCatalogView(BaseApiView):
    """GET /api/v1/events/catalog — event names seen, with volume + last payload."""

    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="events_catalog", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Events"])
    def get(self, request, *args, **kwargs):
        rows = (
            BusinessEvent.objects.filter(account=request.user)
            .values("name")
            .annotate(count=Count("id"), last_seen=Max("received_at"))
            .order_by("-count")
        )
        catalog = []
        for r in rows:
            sample = (
                BusinessEvent.objects.filter(account=request.user, name=r["name"])
                .order_by("-received_at")
                .values_list("data", flat=True)
                .first()
            )
            catalog.append({
                "name": r["name"],
                "count": r["count"],
                "last_seen": r["last_seen"],
                "sample_payload": sample,
            })
        return Response({"data": catalog})


class WebhookTestView(BaseApiView):
    """POST /api/v1/webhooks/test — fan a synthetic event to your endpoints.

    Body: {"event": "contact.created", "data": {...}}. Fires through the normal
    signed-delivery path so you can verify your receiver end to end.
    """

    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="webhooks_test", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Webhooks"])
    def post(self, request, *args, **kwargs):
        from apps.email.models import WebhookEndpoint
        from apps.email.webhooks import notify

        d = request.data if isinstance(request.data, dict) else {}
        event_type = d.get("event", "webhook.test")
        valid = {c[0] for c in WebhookEndpoint.EVENT_CHOICES} | {"webhook.test"}
        if event_type not in valid:
            return Response(
                {"error": {"code": "validation_error",
                           "message": f"unknown event type {event_type!r}"}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payload = d.get("data") or {"ping": True}
        delivery_ids = notify(request.user, event_type, {**payload, "test": True})
        return Response(
            {"event": event_type, "deliveries": len(delivery_ids)},
            status=status.HTTP_202_ACCEPTED,
        )
