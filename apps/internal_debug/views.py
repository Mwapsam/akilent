"""Read-only internal debug API, plus one safe replay action.

Everything here is gated by INTERNAL_DEBUG_ENABLED (urls.py is only included
when the flag is on) and InternalDebugTokenAuthentication. Responses never
include secret fields (access_token, verification_pin) — only booleans about
whether they're set.
"""
from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.internal_debug.authentication import InternalDebugTokenAuthentication
from apps.internal_debug.permissions import IsInternalDebugAuthenticated

logger = logging.getLogger("internal_debug")


class _InternalDebugView(APIView):
    authentication_classes = [InternalDebugTokenAuthentication]
    permission_classes = [IsInternalDebugAuthenticated]
    versioning_class = None  # bypass apps.api's global URLPathVersioning (v1-only)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        logger.info(
            "internal_debug: %s %s params=%s",
            request.method, request.path, request.query_params.dict(),
        )


class WhatsAppNumbersView(_InternalDebugView):
    def get(self, request, *args, **kwargs):
        from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

        account_id = request.query_params.get("account_id")
        if not account_id:
            return Response(
                {"error": "account_id is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        numbers = WhatsAppBusinessNumber.objects.filter(account_id=account_id).order_by("-created_at")
        data = [
            {
                "id": n.pk,
                "phone_number_id": n.phone_number_id,
                "waba_id": n.waba_id,
                "business_id": n.business_id,
                "display_number": n.display_number,
                "is_active": n.is_active,
                "has_access_token": bool(n.access_token),
                "has_verification_pin": bool(n.verification_pin),
                "send_rate_limit": n.send_rate_limit,
                "created_at": n.created_at.isoformat(),
                "updated_at": n.updated_at.isoformat(),
            }
            for n in numbers
        ]
        return Response({"results": data})


class WebhookEventsView(_InternalDebugView):
    def get(self, request, *args, **kwargs):
        from apps.whatsapp.models import WebhookEventLog

        qs = WebhookEventLog.objects.all().order_by("-created_at")

        phone_number_id = request.query_params.get("phone_number_id")
        if phone_number_id:
            qs = qs.filter(payload__entry__0__changes__0__value__metadata__phone_number_id=phone_number_id)

        processed = request.query_params.get("processed")
        if processed is not None:
            qs = qs.filter(processed=processed.lower() == "true")

        try:
            limit = min(int(request.query_params.get("limit", 50)), 200)
        except ValueError:
            limit = 50

        data = [
            {
                "id": e.pk,
                "source": e.source,
                "event_type": e.event_type,
                "processed": e.processed,
                "attempts": e.attempts,
                "error_message": e.error_message,
                "created_at": e.created_at.isoformat(),
                "processed_at": e.processed_at.isoformat() if e.processed_at else None,
                "payload_preview": str(e.payload)[:500],
            }
            for e in qs[:limit]
        ]
        return Response({"results": data})


class WebhookEventResendView(_InternalDebugView):
    def post(self, request, event_id, *args, **kwargs):
        from apps.whatsapp.models import WebhookEventLog
        from apps.whatsapp.tasks import process_whatsapp_event

        try:
            event = WebhookEventLog.objects.get(pk=event_id)
        except WebhookEventLog.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        process_whatsapp_event.delay(event.id)
        logger.info("internal_debug: resent webhook event %s", event.id)
        return Response({"ok": True, "event_id": event.id})


class OnboardingStateView(_InternalDebugView):
    def get(self, request, *args, **kwargs):
        from apps.accounts.models import Account
        from apps.accounts.onboarding import get_state

        account_id = request.query_params.get("account_id")
        if not account_id:
            return Response(
                {"error": "account_id is required"}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            account = Account.objects.get(pk=account_id)
        except Account.DoesNotExist:
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        state = get_state(account)
        state["onboarding_state"] = account.onboarding_state
        return Response(state)


class InternalPageView(_InternalDebugView):
    """Renders an internal/staff page server-side and returns its HTML.

    Uses Django's test client with a fixed internal-debug superuser so the
    MCP client can inspect what's served without real browser cookies. Public
    marketing pages don't need this — fetch them directly over HTTPS instead.
    """

    def get(self, request, *args, **kwargs):
        from django.contrib.auth import get_user_model
        from django.test import Client

        path = request.query_params.get("path")
        if not path or not path.startswith("/"):
            return Response(
                {"error": "path is required and must start with '/'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        User = get_user_model()
        user = User.objects.filter(is_superuser=True).order_by("pk").first()
        if user is None:
            return Response(
                {"error": "no superuser available to render as"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        client = Client()
        client.force_login(user)
        response = client.get(path)
        return Response(
            {
                "status_code": response.status_code,
                "content": response.content.decode(errors="replace"),
            }
        )
