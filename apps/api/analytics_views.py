"""Public API for aggregated send analytics (reads the MessageStatsDaily rollup)."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

from apps.api.base import BaseApiView
from apps.api.permissions import HasEmailApiFeature
from apps.logs.models import MessageStatsDaily

_MEASURES = (
    "sent", "delivered", "bounced", "complained", "rejected",
    "opened", "clicked", "unique_opens", "unique_clicks", "unsubscribed",
)
_GROUP_FIELDS = {
    "day": "day",
    "template": "template__slug",
    "campaign": "campaign_id",
    "domain": "domain__domain",
}


class AnalyticsView(BaseApiView):
    """GET /api/v1/analytics?group_by=day|template|campaign|domain&from=&to=&key_mode=

    Rows come from the daily rollup, summed over the requested window and grouped
    by one dimension. ``from``/``to`` are ISO dates (default: trailing 30 days).
    """

    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="analytics", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Analytics"])
    def get(self, request, *args, **kwargs):
        group_by = request.query_params.get("group_by", "day")
        field = _GROUP_FIELDS.get(group_by)
        if field is None:
            return Response(
                {"error": {"code": "validation_error",
                           "message": f"group_by must be one of {sorted(_GROUP_FIELDS)}"}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        today = timezone.now().date()
        date_to = parse_date(request.query_params.get("to", "")) or today
        date_from = parse_date(request.query_params.get("from", "")) or (today - timedelta(days=30))
        if date_from > date_to:
            return Response(
                {"error": {"code": "validation_error", "message": "`from` is after `to`"}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        key_mode = request.query_params.get("key_mode", "live")
        qs = MessageStatsDaily.objects.filter(
            account=request.user, key_mode=key_mode,
            day__gte=date_from, day__lte=date_to,
        )
        rows = (
            qs.values(field)
            .annotate(**{m: Sum(m) for m in _MEASURES})
            .order_by(field)
        )
        data = []
        for r in rows:
            measures = {m: (r.get(m) or 0) for m in _MEASURES}
            data.append({"group": r.get(field), **measures})

        totals = qs.aggregate(**{m: Sum(m) for m in _MEASURES})
        return Response({
            "group_by": group_by,
            "from": date_from,
            "to": date_to,
            "key_mode": key_mode,
            "totals": {m: (totals.get(m) or 0) for m in _MEASURES},
            "data": data,
        })
