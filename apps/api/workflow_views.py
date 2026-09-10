"""Public API for lifecycle Workflows (Phase 6 / Release 3)."""
from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

from apps.api.base import BaseApiView
from apps.api.permissions import HasEmailApiFeature
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import enroll, validate_definition
from apps.contacts.models import Contact


def _workflow_dict(w: Workflow, *, with_definition: bool = False) -> dict:
    d = {
        "id": w.slug,
        "name": w.name,
        "status": w.status,
        "version": w.version,
        "run_count": w.runs.count(),
        "created_at": w.created_at,
        "updated_at": w.updated_at,
    }
    if with_definition:
        d["definition"] = w.definition
    return d


def _run_dict(r: WorkflowRun, *, with_steps: bool = False) -> dict:
    d = {
        "id": r.public_id,
        "workflow": r.workflow.slug,
        "contact": r.contact.public_id,
        "status": r.status,
        "current_step": r.current_step,
        "next_due_at": r.next_due_at,
        "started_at": r.started_at,
        "completed_at": r.completed_at,
    }
    if with_steps:
        d["steps"] = [
            {
                "step_id": s.step_id,
                "step_type": s.step_type,
                "status": s.status,
                "result": s.result,
                "executed_at": s.executed_at,
            }
            for s in r.step_runs.all()
        ]
    return d


class WorkflowCollectionView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="workflows_list", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def get(self, request, *args, **kwargs):
        qs = Workflow.objects.filter(account=request.user)
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        return Response({"data": [_workflow_dict(w) for w in qs]})

    @extend_schema(operation_id="workflows_create", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def post(self, request, *args, **kwargs):
        d = request.data if isinstance(request.data, dict) else {}
        name = d.get("name")
        if not name:
            return Response({"error": {"code": "validation_error", "message": "name is required"}},
                            status=status.HTTP_400_BAD_REQUEST)
        definition = d.get("definition") or {}
        w = Workflow.objects.create(account=request.user, name=name, definition=definition)
        request.auth.touch()
        return Response(_workflow_dict(w, with_definition=True), status=status.HTTP_201_CREATED)


class WorkflowDetailView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    def _get(self, request, slug):
        return Workflow.objects.get(account=request.user, slug=slug)

    @extend_schema(operation_id="workflows_retrieve", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def get(self, request, slug, *args, **kwargs):
        return Response(_workflow_dict(self._get(request, slug), with_definition=True))

    @extend_schema(operation_id="workflows_update", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def patch(self, request, slug, *args, **kwargs):
        w = self._get(request, slug)
        d = request.data if isinstance(request.data, dict) else {}
        fields = []
        if "name" in d:
            w.name = d["name"]
            fields.append("name")
        if "definition" in d:
            w.definition = d["definition"] or {}
            fields.append("definition")
        if fields:
            w.save(update_fields=[*fields, "updated_at"])
        return Response(_workflow_dict(w, with_definition=True))

    @extend_schema(operation_id="workflows_delete", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def delete(self, request, slug, *args, **kwargs):
        self._get(request, slug).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkflowPublishView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="workflows_publish", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def post(self, request, slug, *args, **kwargs):
        w = Workflow.objects.get(account=request.user, slug=slug)
        errors = validate_definition(w.definition)
        if errors:
            return Response({"error": {"code": "invalid_workflow",
                                       "message": "workflow definition is invalid",
                                       "details": errors}},
                            status=status.HTTP_400_BAD_REQUEST)
        if w.status != Workflow.Status.PUBLISHED:
            w.version += 1
        w.status = Workflow.Status.PUBLISHED
        w.save(update_fields=["status", "version", "updated_at"])
        return Response(_workflow_dict(w))


class WorkflowArchiveView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="workflows_archive", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def post(self, request, slug, *args, **kwargs):
        w = Workflow.objects.get(account=request.user, slug=slug)
        w.status = Workflow.Status.ARCHIVED
        w.save(update_fields=["status", "updated_at"])
        return Response(_workflow_dict(w))


class WorkflowRunsView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="workflows_runs_list", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def get(self, request, slug, *args, **kwargs):
        w = Workflow.objects.get(account=request.user, slug=slug)
        qs = w.runs.select_related("contact")
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        try:
            limit = min(int(request.query_params.get("limit", 50)), 200)
        except (TypeError, ValueError):
            limit = 50
        return Response({"data": [_run_dict(r) for r in qs[:limit]]})

    @extend_schema(operation_id="workflows_enroll", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def post(self, request, slug, *args, **kwargs):
        w = Workflow.objects.get(account=request.user, slug=slug)
        if w.status != Workflow.Status.PUBLISHED:
            return Response({"error": {"code": "workflow_not_published",
                                       "message": "publish the workflow before enrolling contacts"}},
                            status=status.HTTP_409_CONFLICT)
        d = request.data if isinstance(request.data, dict) else {}
        ref = d.get("contact")
        if not ref:
            return Response({"error": {"code": "validation_error", "message": "contact is required"}},
                            status=status.HTTP_400_BAD_REQUEST)
        if ref.startswith("con_"):
            contact = Contact.objects.get(account=request.user, public_id=ref)
        else:
            contact = Contact.objects.get(account=request.user, email__iexact=ref)
        run = enroll(w, contact, context=d.get("context") or {})
        if run is None:
            return Response({"error": {"code": "enroll_failed",
                                       "message": "workflow could not enroll this contact"}},
                            status=status.HTTP_409_CONFLICT)
        return Response(_run_dict(run, with_steps=True), status=status.HTTP_202_ACCEPTED)


class WorkflowRunDetailView(BaseApiView):
    permission_classes = [HasEmailApiFeature]

    def _get(self, request, run_id):
        return WorkflowRun.objects.select_related("workflow", "contact").get(
            workflow__account=request.user, public_id=run_id
        )

    @extend_schema(operation_id="workflow_runs_retrieve", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def get(self, request, run_id, *args, **kwargs):
        return Response(_run_dict(self._get(request, run_id), with_steps=True))

    @extend_schema(operation_id="workflow_runs_cancel", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Workflows"])
    def delete(self, request, run_id, *args, **kwargs):
        run = self._get(request, run_id)
        if run.status in (WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING):
            run.status = WorkflowRun.Status.CANCELLED
            run.next_due_at = None
            run.save(update_fields=["status", "next_due_at"])
        return Response(_run_dict(run))
