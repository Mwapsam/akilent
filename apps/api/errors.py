"""Standardized {"error": {"code", "message"}} envelope for apps.api.

Wraps DRF's default exception handler so every error response — validation,
auth, throttling, permission, and the billing PlanLimitExceeded exception
that isn't a DRF exception at all — comes back in one predictable shape.
"""
from __future__ import annotations

from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from apps.api.services import (
    RecipientCapExceededError,
    TemplateMissingContentError,
    UnverifiedDomainError,
)
from apps.billing.limits import PlanLimitExceeded
from apps.email.models import BulkEmailCampaign, EmailMessage, EmailTemplate


def _flatten_detail(data) -> str:
    if isinstance(data, dict):
        if set(data.keys()) == {"detail"}:
            return str(data["detail"])
        parts = []
        for field, errors in data.items():
            if isinstance(errors, (list, tuple)):
                parts.append(f"{field}: {'; '.join(str(e) for e in errors)}")
            else:
                parts.append(f"{field}: {errors}")
        return " | ".join(parts)
    if isinstance(data, (list, tuple)):
        return "; ".join(str(e) for e in data)
    return str(data)


def _request_id(context) -> str:
    request = (context or {}).get("request")
    return getattr(request, "id", "") or ""


def _envelope(code: str, message: str, status_code: int, context) -> Response:
    from apps.api.error_codes import CATALOG, docs_url

    err = {"code": code, "message": message}
    if code in CATALOG:
        err["docs_url"] = docs_url(code)
    rid = _request_id(context)
    if rid:
        err["request_id"] = rid
    return Response({"error": err}, status=status_code)


def custom_exception_handler(exc, context):
    if isinstance(exc, PlanLimitExceeded):
        return _envelope(exc.limit_type, str(exc), status.HTTP_403_FORBIDDEN, context)

    if isinstance(exc, UnverifiedDomainError):
        return _envelope("unverified_domain", str(exc), status.HTTP_403_FORBIDDEN, context)

    if isinstance(exc, RecipientCapExceededError):
        return _envelope(
            "recipient_cap_exceeded", str(exc), status.HTTP_403_FORBIDDEN, context
        )

    if isinstance(exc, TemplateMissingContentError):
        return _envelope("missing_content", str(exc), status.HTTP_400_BAD_REQUEST, context)

    from apps.email.services.attachments import AttachmentError

    if isinstance(exc, AttachmentError):
        return _envelope(
            "invalid_attachment", str(exc), status.HTTP_400_BAD_REQUEST, context
        )

    if isinstance(exc, EmailTemplate.DoesNotExist):
        return _envelope(
            "not_found", "Template not found.", status.HTTP_404_NOT_FOUND, context
        )

    if isinstance(exc, BulkEmailCampaign.DoesNotExist):
        return _envelope(
            "not_found", "Campaign not found.", status.HTTP_404_NOT_FOUND, context
        )

    if isinstance(exc, EmailMessage.DoesNotExist):
        return _envelope(
            "not_found", "Message not found.", status.HTTP_404_NOT_FOUND, context
        )

    from django.core.exceptions import ObjectDoesNotExist

    if isinstance(exc, ObjectDoesNotExist):
        return _envelope(
            "not_found", "Resource not found.", status.HTTP_404_NOT_FOUND, context
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    code = getattr(exc, "default_code", "error")
    message = _flatten_detail(response.data)
    from apps.api.error_codes import CATALOG, docs_url

    err = {"code": code, "message": message}
    if code in CATALOG:
        err["docs_url"] = docs_url(code)
    rid = _request_id(context)
    if rid:
        err["request_id"] = rid
    response.data = {"error": err}

    if isinstance(exc, drf_exceptions.Throttled):
        response["Retry-After"] = str(int(exc.wait)) if exc.wait else "60"

    return response
