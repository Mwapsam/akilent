"""Public API for WhatsApp campaigns (audit 2026-10-19, Phase 11).

Same rules as the screens — every call goes through apps.whatsapp.campaigns:

    GET  /v1/whatsapp/campaigns                 list (newest first)
    POST /v1/whatsapp/campaigns                 send now, schedule (send_at) or draft
    GET  /v1/whatsapp/campaigns/<id>            status, delivery stats, who wasn't sent to
    POST /v1/whatsapp/campaigns/<id>/<action>   pause · resume · cancel · send_now

Needs the plan's WhatsApp campaigns feature and a key with the
``whatsapp:campaigns`` scope. POST /v1/whatsapp/campaigns honours
``Idempotency-Key``: a retry with the same key returns the same campaign and
never sends twice.
"""

from __future__ import annotations

from datetime import datetime

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from apps.api.base import BaseApiView
from apps.api.permissions import HasScope


class HasWhatsAppCampaigns(BasePermission):
    message = "Your plan does not include WhatsApp campaigns."

    def has_permission(self, request, view) -> bool:
        from apps.billing import api as billing_api
        from apps.core.channels import is_enabled

        account = request.user
        return (
            account is not None
            and is_enabled("whatsapp")
            and billing_api.usable(account, "whatsapp_campaigns")
        )


def _error(code, message, http=status.HTTP_400_BAD_REQUEST):
    return Response({"error": {"code": code, "message": message}}, status=http)


def _campaign_dict(c, *, detail: bool = False) -> dict:
    from apps.whatsapp.campaign_ui import lifecycle_state

    out = {
        "id": c.pk,
        "name": c.name,
        "status": lifecycle_state(c),
        "list": c.contact_list.slug if c.contact_list_id else None,
        "segment": c.segment.slug if c.segment_id else None,
        "template": c.template.whatsapp_template_name,
        "recipients": c.recipient_count,
        "queued": c.queued_count,
        "not_sent": c.skipped_count,
        "scheduled_at": c.scheduled_at,
        "timezone": c.timezone or None,
        "created_at": c.created_at,
        "started_at": c.started_at,
        "completed_at": c.completed_at,
        "error": c.error or None,
    }
    if detail:
        from django.db.models import Count

        from apps.whatsapp.campaigns import campaign_delivery_stats

        out["delivery"] = campaign_delivery_stats(c)
        out["not_sent_reasons"] = {
            row["skip_reason"]: row["n"]
            for row in c.recipients.exclude(skip_reason="")
            .values("skip_reason")
            .annotate(n=Count("id"))
        }
    return out


class WhatsAppCampaignCollectionView(BaseApiView):
    permission_classes = [HasWhatsAppCampaigns, HasScope]
    required_scope = "whatsapp:campaigns"
    idempotency_endpoint = "POST /v1/whatsapp/campaigns"

    @extend_schema(
        operation_id="whatsapp_campaigns_list",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["WhatsApp campaigns"],
    )
    def get(self, request, *args, **kwargs):
        from apps.api.contact_views import _paging
        from apps.whatsapp.models import WhatsAppCampaign

        qs = WhatsAppCampaign.objects.filter(account=request.user).select_related(
            "contact_list", "segment", "template"
        )
        limit, offset = _paging(request)
        return Response(
            {
                "data": [_campaign_dict(c) for c in qs[offset : offset + limit]],
                "total": qs.count(),
                "limit": limit,
                "offset": offset,
            }
        )

    @extend_schema(
        operation_id="whatsapp_campaigns_create",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["WhatsApp campaigns"],
    )
    def post(self, request, *args, **kwargs):
        from apps.contacts.models import ContactList, Segment
        from apps.core.scheduling import InvalidTimezone, to_utc, validate_iana
        from apps.whatsapp import campaigns as svc
        from apps.whatsapp.models import MessageTemplate

        account = request.user
        d = request.data if isinstance(request.data, dict) else {}

        if bool(d.get("list")) == bool(d.get("segment")):
            return _error("validation_error", "Give exactly one of list or segment.")
        contact_list = segment = None
        if d.get("list"):
            contact_list = ContactList.objects.filter(
                account=account, slug=d["list"]
            ).first()
            if contact_list is None:
                return _error(
                    "not_found", "No list with that id.", status.HTTP_404_NOT_FOUND
                )
        else:
            segment = Segment.objects.filter(account=account, slug=d["segment"]).first()
            if segment is None:
                return _error(
                    "not_found", "No segment with that id.", status.HTTP_404_NOT_FOUND
                )

        ref = d.get("template")
        templates = MessageTemplate.objects.filter(account=account)
        template = (
            templates.filter(pk=int(ref)).first()  # type: ignore[arg-type]
            if isinstance(ref, int) or str(ref).isdigit()
            else templates.filter(
                whatsapp_template_name=ref,
                approval_status=MessageTemplate.ApprovalStatus.APPROVED,
            ).first()
        )
        if template is None:
            return _error(
                "not_found",
                "No template with that id or name.",
                status.HTTP_404_NOT_FOUND,
            )

        variables = d.get("variables") or {}
        fallbacks = d.get("fallbacks") or {}
        if not isinstance(variables, dict) or not isinstance(fallbacks, dict):
            return _error(
                "validation_error", "variables and fallbacks must be objects."
            )
        missing = [v for v in template.variables or [] if v not in variables]
        if missing:
            return _error(
                "validation_error",
                "Say what goes in every blank: missing "
                + ", ".join(missing)
                + '. Use e.g. "contact.first_name" or fixed text.',
            )

        send_at = None
        tz = d.get("timezone") or ""
        if d.get("send_at"):
            try:
                parsed = datetime.fromisoformat(
                    str(d["send_at"]).replace("Z", "+00:00")
                )
                if tz:
                    validate_iana(tz)
                send_at = to_utc(parsed, tz or "UTC")
            except (ValueError, InvalidTimezone):
                return _error(
                    "validation_error",
                    "send_at must be an ISO 8601 time and timezone an IANA zone.",
                )

        idem = self.begin_idempotency(request)
        key = (request.headers.get("Idempotency-Key") or "")[:64]
        try:
            if d.get("draft") is True:
                campaign = svc.save_draft(
                    account=account,
                    name=d.get("name") or "",
                    template_id=template.pk,
                    contact_list=contact_list,
                    segment=segment,
                    variable_mapping=variables,
                    variable_fallbacks=fallbacks,
                )
            else:
                campaign = svc.create_and_queue_campaign(
                    account=account,
                    name=d.get("name") or "",
                    template_id=template.pk,
                    contact_list=contact_list,
                    segment=segment,
                    variable_mapping=variables,
                    variable_fallbacks=fallbacks,
                    submission_key=f"api:{key}" if key else "",
                    send_at=send_at,
                    tz=tz,
                )
        except svc.CampaignError as exc:
            self.release_idempotency(idem)
            return _error(
                "campaign_error", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        request.auth.touch()
        return self.finish_idempotency(
            idem,
            Response(
                _campaign_dict(campaign, detail=True), status=status.HTTP_201_CREATED
            ),
        )


class WhatsAppCampaignDetailView(BaseApiView):
    permission_classes = [HasWhatsAppCampaigns, HasScope]
    required_scope = "whatsapp:campaigns"

    @extend_schema(
        operation_id="whatsapp_campaigns_retrieve",
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["WhatsApp campaigns"],
    )
    def get(self, request, pk, *args, **kwargs):
        from apps.whatsapp.models import WhatsAppCampaign

        c = WhatsAppCampaign.objects.select_related(
            "contact_list", "segment", "template"
        ).get(account=request.user, pk=pk)
        return Response(_campaign_dict(c, detail=True))


class WhatsAppCampaignActionView(BaseApiView):
    permission_classes = [HasWhatsAppCampaigns, HasScope]
    required_scope = "whatsapp:campaigns"

    @extend_schema(
        operation_id="whatsapp_campaigns_action",
        request=None,
        responses=OpenApiResponse(OpenApiTypes.OBJECT),
        tags=["WhatsApp campaigns"],
    )
    def post(self, request, pk, action, *args, **kwargs):
        from apps.whatsapp import campaigns as svc
        from apps.whatsapp.models import WhatsAppCampaign

        c = WhatsAppCampaign.objects.get(account=request.user, pk=pk)
        try:
            if action == "pause":
                ok = svc.pause_campaign(c)
            elif action == "resume":
                ok = svc.resume_campaign(c)
            elif action == "cancel":
                ok = svc.can_cancel(c)
                if ok:
                    svc.cancel_campaign(c)
            elif action == "send_now":
                c = svc.start_campaign(c.pk)
                ok = c.status not in WhatsAppCampaign.EDITABLE
            else:
                return _error("not_found", "Unknown action.", status.HTTP_404_NOT_FOUND)
        except svc.CampaignError as exc:
            return _error(
                "campaign_error", str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY
            )
        if not ok:
            return _error(
                "invalid_state",
                f"Can't {action.replace('_', ' ')} a campaign that is {c.get_status_display().lower()}.",
                status.HTTP_409_CONFLICT,
            )
        c.refresh_from_db()
        return Response(_campaign_dict(c, detail=True))
