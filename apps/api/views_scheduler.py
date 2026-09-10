"""Public REST surface for the Akilent Scheduler.

GET    /v1/scheduled-jobs            — list (filters: kind, status, from, to)
GET    /v1/scheduled-jobs/{id}       — detail + resolved target
PATCH  /v1/scheduled-jobs/{id}       — reschedule (scheduled_at + timezone)
DELETE /v1/scheduled-jobs/{id}       — cancel

All account-scoped, scope ``messages:send``, via BaseApiView (idempotency +
request logging inherited).
"""
from __future__ import annotations

from django.utils.dateparse import parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

from apps.api.base import BaseApiView
from apps.api.permissions import HasEmailApiFeature, HasScope
from apps.scheduler.api import SchedulingError, cancel_job, list_jobs, reschedule_job
from apps.scheduler.models import ScheduledJob


def _job_dict(job: ScheduledJob, *, detail: bool = False) -> dict:
    body = {
        "id": job.public_id,
        "kind": job.kind,
        "status": job.status,
        "fire_at": job.fire_at,
        "timezone": job.tz,
        "recurrence": job.recurrence or None,
        "recurrence_until": job.recurrence_until,
        "max_occurrences": job.max_occurrences,
        "occurrence_count": job.occurrence_count,
        "attempts": job.attempts,
        "error": job.error or None,
        "created_at": job.created_at,
        "fired_at": job.fired_at,
    }
    if detail:
        target = None
        if job.target_message_id:
            target = {"type": "message", "id": job.target_message.public_id}
        elif job.target_campaign_id:
            camp = job.target_campaign
            target = {
                "type": "campaign",
                "id": camp.id,
                "status": camp.status,
                "recipient_count": camp.recipient_count,
            }
        elif job.target_outbound_id:
            target = {"type": "whatsapp", "id": job.target_outbound_id,
                      "status": job.target_outbound.status}
        body["target"] = target
        body["result"] = job.result or {}
    return body


class ScheduledJobCollectionView(BaseApiView):
    permission_classes = [HasEmailApiFeature, HasScope]
    required_scope = "messages:send"

    @extend_schema(
        operation_id="scheduled_jobs_list",
        responses=OpenApiResponse(OpenApiTypes.OBJECT, "Paginated list of scheduled jobs."),
        tags=["Scheduler"],
    )
    def get(self, request, *args, **kwargs):
        qs = list_jobs(
            request.user,
            kind=request.query_params.get("kind") or None,
            status=request.query_params.get("status") or None,
            since=parse_datetime(request.query_params.get("from") or ""),
            until=parse_datetime(request.query_params.get("to") or ""),
        )
        try:
            limit = min(int(request.query_params.get("limit", 50)), 100)
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            offset = 0
        total = qs.count()
        rows = list(qs[offset:offset + limit])
        request.auth.touch()
        return Response({
            "data": [_job_dict(j) for j in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        })


class ScheduledJobDetailView(BaseApiView):
    permission_classes = [HasEmailApiFeature, HasScope]
    required_scope = "messages:send"

    def _get(self, request, public_id) -> ScheduledJob:
        return ScheduledJob.objects.select_related(
            "target_message", "target_campaign", "target_outbound"
        ).get(account=request.user, public_id=public_id, parent__isnull=True)

    @extend_schema(operation_id="scheduled_job_detail", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Scheduler"])
    def get(self, request, public_id, *args, **kwargs):
        job = self._get(request, public_id)
        request.auth.touch()
        return Response(_job_dict(job, detail=True))

    @extend_schema(operation_id="scheduled_job_reschedule", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Scheduler"])
    def patch(self, request, public_id, *args, **kwargs):
        job = self._get(request, public_id)
        scheduled_at = parse_datetime(str(request.data.get("scheduled_at") or ""))
        if scheduled_at is None:
            return Response(
                {"error": {"code": "invalid_schedule",
                           "message": "scheduled_at (ISO 8601) is required"}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            reschedule_job(job, scheduled_at=scheduled_at,
                           tz=request.data.get("timezone") or "")
        except SchedulingError as exc:
            code = status.HTTP_409_CONFLICT if job.status != ScheduledJob.Status.SCHEDULED else status.HTTP_400_BAD_REQUEST
            return Response(
                {"error": {"code": "invalid_schedule", "message": str(exc)}}, status=code
            )
        request.auth.touch()
        return Response(_job_dict(job, detail=True))

    @extend_schema(operation_id="scheduled_job_cancel", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Scheduler"])
    def delete(self, request, public_id, *args, **kwargs):
        job = self._get(request, public_id)
        try:
            cancel_job(job)
        except SchedulingError as exc:
            return Response(
                {"error": {"code": "invalid_schedule", "message": str(exc)}},
                status=status.HTTP_409_CONFLICT,
            )
        request.auth.touch()
        return Response(_job_dict(job, detail=True))
