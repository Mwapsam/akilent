from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

from apps.api.base import BaseApiView
from apps.api.permissions import (
    HasBulkEmailFeature,
    HasEmailApiFeature,
    HasEmailTemplatesFeature,
    HasScope,
)
from apps.api.serializers import (
    CampaignCreateSerializer,
    MessageCreateSerializer,
    TemplateSerializer,
)
from apps.api.services import (
    clone_template,
    create_and_queue_campaign,
    create_and_queue_message,
    create_template,
    render_template_preview,
    update_template,
)
from apps.email.models import BulkEmailCampaign, EmailMessage, EmailTemplate
from apps.logs.models import ApiRequest


def _paging(request, *, default=50, cap=100):
    try:
        limit = min(int(request.query_params.get("limit", default)), cap)
    except (TypeError, ValueError):
        limit = default
    try:
        offset = max(int(request.query_params.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    return max(limit, 1), offset


def _message_summary(m):
    return {
        "id": m.public_id,
        "status": m.status,
        "from": m.from_email,
        "to": m.to_email,
        "subject": m.subject,
        "template": m.template.slug if m.template_id else None,
        "campaign_id": m.campaign_id,
        "created_at": m.created_at,
        "sent_at": m.sent_at,
    }


def _event_dict(e):
    return {
        "id": e.public_id,
        "type": e.type,
        "source": e.source,
        "occurred_at": e.occurred_at,
        "recorded_at": e.recorded_at,
        "data": e.data,
        "request_id": e.request_id or None,
    }


class MessageCreateView(BaseApiView):
    """POST /api/v1/messages — send a transactional email.

    This is the endpoint marketed on the landing page's Developer Platform
    section. request.user is the authenticated Account and request.auth is
    the EmailApiKey (see EmailApiKeyAuthentication).
    """

    permission_classes = [HasEmailApiFeature]
    idempotency_endpoint = "POST /v1/messages"

    @extend_schema(
        operation_id="messages_list",
        responses=OpenApiResponse(OpenApiTypes.OBJECT, "Paginated list of sent messages."),
        tags=["Messages"],
    )
    def get(self, request, *args, **kwargs):
        return _message_list_response(request)

    @extend_schema(
        operation_id="messages_send",
        request=MessageCreateSerializer,
        responses={202: OpenApiResponse(OpenApiTypes.OBJECT, "Message accepted for delivery.")},
        tags=["Messages"],
    )
    def post(self, request, *args, **kwargs):
        idem = self.begin_idempotency(request)
        serializer = MessageCreateSerializer(
            data=MessageCreateSerializer.from_request_data(request.data)
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        msg = create_and_queue_message(
            account=request.user,
            from_email=data["from_email"],
            to_email=data["to_email"],
            subject=data.get("subject", ""),
            text_body=data.get("text", ""),
            html_body=data.get("html", ""),
            template_id=data.get("template_id"),
            template_variables=data.get("template_variables"),
            mode=getattr(request.auth, "mode", "live"),
        )
        request.auth.touch()
        return self.finish_idempotency(
            idem,
            Response(
                {"id": msg.id, "public_id": msg.public_id, "status": msg.status},
                status=status.HTTP_202_ACCEPTED,
            ),
        )


class TemplateListCreateView(BaseApiView):
    """GET /api/v1/templates — list; POST /api/v1/templates — create."""

    permission_classes = [HasEmailApiFeature, HasEmailTemplatesFeature, HasScope]
    required_scope = "templates:manage"

    @extend_schema(operation_id="template_list_create_get", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["API"])
    def get(self, request, *args, **kwargs):
        templates = EmailTemplate.objects.filter(account=request.user, is_active=True)
        return Response(
            [
                {
                    "id": t.id,
                    "name": t.name,
                    "slug": t.slug,
                    "subject": t.subject,
                    "updated_at": t.updated_at,
                }
                for t in templates
            ]
        )

    @extend_schema(operation_id="template_create", request=TemplateSerializer, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Templates"])
    def post(self, request, *args, **kwargs):
        serializer = TemplateSerializer(
            data=TemplateSerializer.from_request_data(request.data)
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        template = create_template(
            account=request.user,
            name=data["name"],
            slug=data.get("slug", ""),
            subject=data.get("subject", ""),
            text_body=data.get("text", ""),
            html_body=data.get("html", ""),
            sample_variables=data.get("sample_variables"),
            content_blocks=data.get("content_blocks"),
            builder_mode=data.get("builder_mode", "raw"),
        )
        request.auth.touch()
        return Response(
            {"id": template.id, "name": template.name, "slug": template.slug},
            status=status.HTTP_201_CREATED,
        )


class TemplateDetailView(BaseApiView):
    """GET/PATCH/DELETE /api/v1/templates/<slug>."""

    permission_classes = [HasEmailApiFeature, HasEmailTemplatesFeature, HasScope]
    required_scope = "templates:manage"

    def _get_template(self, request, slug):
        return EmailTemplate.objects.get(account=request.user, slug=slug)

    @extend_schema(operation_id="template_detail_get", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["API"])
    def get(self, request, slug, *args, **kwargs):
        t = self._get_template(request, slug)
        return Response(
            {
                "id": t.id,
                "name": t.name,
                "slug": t.slug,
                "subject": t.subject,
                "text": t.text_body,
                "html": t.html_body,
                "sample_variables": t.sample_variables,
                "content_blocks": t.content_blocks,
                "builder_mode": t.builder_mode,
            }
        )

    @extend_schema(operation_id="template_update", request=TemplateSerializer, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Templates"])
    def patch(self, request, slug, *args, **kwargs):
        t = self._get_template(request, slug)
        data = TemplateSerializer.from_request_data(request.data)
        update_template(
            template=t,
            name=request.data.get("name"),
            subject=data.get("subject") if "subject" in request.data else None,
            text=data.get("text") if "text" in request.data else None,
            html=data.get("html") if "html" in request.data else None,
            sample_variables=request.data.get("sample_variables"),
            content_blocks=request.data.get("content_blocks"),
            builder_mode=request.data.get("builder_mode"),
        )
        return Response({"id": t.id, "name": t.name, "slug": t.slug})

    @extend_schema(operation_id="template_detail_delete", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["API"])
    def delete(self, request, slug, *args, **kwargs):
        t = self._get_template(request, slug)
        t.is_active = False
        t.save(update_fields=["is_active"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class TemplateRenderView(BaseApiView):
    """POST /api/v1/templates/<slug>/render — render subject/text/html with variables."""

    permission_classes = [HasEmailApiFeature, HasEmailTemplatesFeature, HasScope]
    required_scope = "templates:manage"

    @extend_schema(operation_id="template_render", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Templates"])
    def post(self, request, slug, *args, **kwargs):
        template = EmailTemplate.objects.get(account=request.user, slug=slug)
        variables = request.data.get("variables") if isinstance(request.data, dict) else None
        result = render_template_preview(template=template, variables=variables)
        request.auth.touch()
        return Response(result)


class TemplatePreviewView(TemplateRenderView):
    """POST /api/v1/templates/<slug>/preview — alias of render, for CI pipelines."""

    @extend_schema(operation_id="template_preview", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Templates"])
    def post(self, request, slug, *args, **kwargs):
        return super().post(request, slug, *args, **kwargs)


class TemplateCloneView(BaseApiView):
    """POST /api/v1/templates/<slug>/clone — duplicate a template."""

    permission_classes = [HasEmailApiFeature, HasEmailTemplatesFeature, HasScope]
    required_scope = "templates:manage"

    @extend_schema(operation_id="template_clone", request=None, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Templates"])
    def post(self, request, slug, *args, **kwargs):
        template = EmailTemplate.objects.get(account=request.user, slug=slug)
        clone = clone_template(template=template)
        request.auth.touch()
        return Response(
            {"id": clone.id, "name": clone.name, "slug": clone.slug},
            status=status.HTTP_201_CREATED,
        )


class CampaignCreateView(BaseApiView):
    """POST /api/v1/campaigns — launch a bulk send."""

    permission_classes = [HasEmailApiFeature, HasBulkEmailFeature, HasScope]
    required_scope = "messages:send:bulk"
    idempotency_endpoint = "POST /v1/campaigns"

    @extend_schema(operation_id="campaign_create", request=CampaignCreateSerializer, responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["Campaigns"])
    def post(self, request, *args, **kwargs):
        idem = self.begin_idempotency(request)
        serializer = CampaignCreateSerializer(
            data=CampaignCreateSerializer.from_request_data(request.data)
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        campaign = create_and_queue_campaign(
            account=request.user,
            from_email=data["from_email"],
            template_id=data.get("template_id"),
            subject=data.get("subject", ""),
            text_body=data.get("text", ""),
            html_body=data.get("html", ""),
            recipients=data["recipients"],
        )
        request.auth.touch()
        return self.finish_idempotency(
            idem,
            Response(
                {
                    "id": campaign.id,
                    "status": campaign.status,
                    "recipient_count": campaign.recipient_count,
                },
                status=status.HTTP_202_ACCEPTED,
            ),
        )


class CampaignDetailView(BaseApiView):
    """GET /api/v1/campaigns/<id> — status/progress poll."""

    permission_classes = [HasEmailApiFeature, HasBulkEmailFeature, HasScope]
    required_scope = "messages:send:bulk"

    @extend_schema(operation_id="campaign_detail_get", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["API"])
    def get(self, request, pk, *args, **kwargs):
        campaign = BulkEmailCampaign.objects.get(pk=pk, account=request.user)
        return Response(
            {
                "id": campaign.id,
                "status": campaign.status,
                "recipient_count": campaign.recipient_count,
                "queued_count": campaign.queued_count,
                "sent_count": campaign.sent_count,
                "failed_count": campaign.failed_count,
            }
        )


def _message_list_response(request):
    qs = (
        EmailMessage.objects.filter(account=request.user)
        .select_related("template")
        .order_by("-created_at")
    )
    p = request.query_params
    if p.get("status"):
        qs = qs.filter(status=p["status"])
    if p.get("to"):
        qs = qs.filter(to_email__iexact=p["to"])
    if p.get("template"):
        qs = qs.filter(template__slug=p["template"])
    if p.get("campaign_id"):
        qs = qs.filter(campaign_id=p["campaign_id"])
    if p.get("domain"):
        qs = qs.filter(domain__domain=p["domain"])

    limit, offset = _paging(request)
    total = qs.count()
    rows = list(qs[offset : offset + limit])
    return Response(
        {
            "data": [_message_summary(m) for m in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


class MessageDetailView(BaseApiView):
    """GET /api/v1/messages/<public_id> — full record + rendered content."""

    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="message_detail_get", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["API"])
    def get(self, request, public_id, *args, **kwargs):
        m = EmailMessage.objects.select_related("template", "domain").get(
            account=request.user, public_id=public_id
        )
        body = _message_summary(m)
        body.update(
            {
                "provider_message_id": m.provider_message_id,
                "error": m.error or None,
                "rendered": {
                    "subject": m.rendered_subject,
                    "text": m.rendered_text,
                    "html": m.rendered_html,
                },
                "events": [
                    _event_dict(e) for e in m.events.order_by("occurred_at", "id")
                ],
            }
        )
        return Response(body)


class MessageEventsView(BaseApiView):
    """GET /api/v1/messages/<public_id>/events — the lifecycle timeline."""

    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="message_events_get", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["API"])
    def get(self, request, public_id, *args, **kwargs):
        m = EmailMessage.objects.get(account=request.user, public_id=public_id)
        events = m.events.order_by("occurred_at", "id")
        return Response({"data": [_event_dict(e) for e in events]})


class RequestLogListView(BaseApiView):
    """GET /api/v1/request-logs — recent API calls for this account."""

    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="request_log_list_get", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["API"])
    def get(self, request, *args, **kwargs):
        qs = ApiRequest.objects.filter(account=request.user).order_by("-created_at")
        p = request.query_params
        if p.get("status_code"):
            qs = qs.filter(status_code=p["status_code"])
        if p.get("path"):
            qs = qs.filter(path__icontains=p["path"])
        if p.get("request_id"):
            qs = qs.filter(request_id=p["request_id"])

        limit, offset = _paging(request)
        total = qs.count()
        rows = list(qs[offset : offset + limit])
        return Response(
            {
                "data": [
                    {
                        "id": r.public_id,
                        "method": r.method,
                        "path": r.path,
                        "status_code": r.status_code,
                        "error_code": r.error_code or None,
                        "request_id": r.request_id or None,
                        "latency_ms": r.latency_ms,
                        "idempotency_replayed": r.idempotency_replayed,
                        "created_at": r.created_at,
                    }
                    for r in rows
                ],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )


class RequestLogDetailView(BaseApiView):
    """GET /api/v1/request-logs/<request_id> — full request/response snapshot."""

    permission_classes = [HasEmailApiFeature]

    @extend_schema(operation_id="request_log_detail_get", responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["API"])
    def get(self, request, request_id, *args, **kwargs):
        r = (
            ApiRequest.objects.filter(account=request.user)
            .filter(Q(request_id=request_id) | Q(public_id=request_id))
            .order_by("-created_at")
            .first()
        )
        if r is None:
            return Response(
                {"error": {"code": "not_found", "message": "Request log not found."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "id": r.public_id,
                "method": r.method,
                "path": r.path,
                "version": r.version,
                "status_code": r.status_code,
                "error_code": r.error_code or None,
                "request_id": r.request_id or None,
                "idempotency_key": r.idempotency_key or None,
                "idempotency_replayed": r.idempotency_replayed,
                "latency_ms": r.latency_ms,
                "request_headers": r.request_headers,
                "request_body": r.request_body,
                "response_body": r.response_body,
                "client_ip": r.client_ip,
                "user_agent": r.user_agent,
                "created_at": r.created_at,
            }
        )
