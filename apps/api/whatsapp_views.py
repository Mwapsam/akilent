"""WhatsApp one-time codes for other systems (login, sign-up, payment confirmation).

The caller's app makes the code and checks it later; Akilent only delivers it with the business's
approved Authentication template. Keys with ``mode="test"`` check the request but send nothing.
"""
from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.response import Response

from apps.api.base import BaseApiView
from apps.api.permissions import HasScope, HasWhatsAppModule
from apps.whatsapp import api as whatsapp_api


class VerificationCodeSerializer(serializers.Serializer):
    to = serializers.CharField(max_length=32, help_text="Phone number with country code, e.g. +260971234567.")
    code = serializers.CharField(max_length=15, help_text="The code your system made: 4-15 letters or numbers.")
    template = serializers.CharField(max_length=255, required=False, allow_blank=True,
                                     help_text="Authentication template name. Defaults to the newest approved one.")
    language = serializers.CharField(max_length=10, required=False, allow_blank=True,
                                     help_text="Template language code, e.g. en. Needed only if the name exists in several.")


class VerificationCodeCreateView(BaseApiView):
    """POST /api/v1/whatsapp/verification-codes — deliver a one-time code on WhatsApp."""

    permission_classes = [HasWhatsAppModule, HasScope]
    required_scope = "messages:send"
    idempotency_endpoint = "POST /v1/whatsapp/verification-codes"

    @extend_schema(
        operation_id="whatsapp_verification_code_send",
        request=VerificationCodeSerializer,
        responses={202: OpenApiResponse(OpenApiTypes.OBJECT, "Code accepted for delivery.")},
        tags=["WhatsApp"],
    )
    def post(self, request, *args, **kwargs):
        idem = self.begin_idempotency(request)
        serializer = VerificationCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        result = whatsapp_api.send_verification_code(
            request.user,
            phone=data["to"],
            code=data["code"],
            template_name=data.get("template", ""),
            language=data.get("language", ""),
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            dry_run=getattr(request.auth, "mode", "live") == "test",
        )
        request.auth.touch()
        return self.finish_idempotency(idem, Response(result, status=status.HTTP_202_ACCEPTED))


class VerificationCodeDetailView(BaseApiView):
    """GET /api/v1/whatsapp/verification-codes/<id> — queued, sent, delivered, read or failed."""

    permission_classes = [HasWhatsAppModule, HasScope]
    required_scope = "messages:send"

    @extend_schema(operation_id="whatsapp_verification_code_status",
                   responses=OpenApiResponse(OpenApiTypes.OBJECT), tags=["WhatsApp"])
    def get(self, request, message_id, *args, **kwargs):
        result = whatsapp_api.verification_code_status(request.user, message_id)
        if result is None:
            return Response({"error": {"code": "not_found", "message": "No code with that id."}},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(result)
