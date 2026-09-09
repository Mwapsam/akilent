"""Dashboard: Email Logs + Request logs (Phase 2 / Epic P0.2)."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from apps.accounts.utils import get_current_account
from apps.email.models import EmailMessage, WebhookDelivery
from apps.logs.models import ApiRequest, MessageEvent

_PAGE_SIZE = 50
_STATUS_CHOICES = EmailMessage.Status.choices


@login_required
def message_list(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    qs = (
        EmailMessage.objects.filter(account=account)
        .select_related("template", "domain")
        .order_by("-created_at")
    )
    q = (request.GET.get("q") or "").strip()
    st = (request.GET.get("status") or "").strip()
    if q:
        qs = qs.filter(to_email__icontains=q)
    if st:
        qs = qs.filter(status=st)

    page = Paginator(qs, _PAGE_SIZE).get_page(request.GET.get("page"))
    total_ever = qs.model.objects.filter(account=account).exists() if (q or st) else qs.exists()
    return render(request, "logs/messages.html", {
        "account": account,
        "page": page,
        "q": q,
        "status": st,
        "status_choices": _STATUS_CHOICES,
        "show_quickstart": not total_ever,
    })


@login_required
def message_detail(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    try:
        msg = (
            EmailMessage.objects.select_related("template", "domain", "campaign")
            .get(account=account, public_id=public_id)
        )
    except EmailMessage.DoesNotExist:
        return render(request, "logs/not_found.html", status=404)

    events = msg.events.order_by("occurred_at", "id")
    api_request = None
    rid = events.values_list("request_id", flat=True).first()
    if rid:
        api_request = ApiRequest.objects.filter(account=account, request_id=rid).first()
    webhook_deliveries = (
        WebhookDelivery.objects.filter(message=msg)
        .select_related("endpoint")
        .order_by("-created_at")
    )
    return render(request, "logs/message_detail.html", {
        "account": account,
        "msg": msg,
        "events": events,
        "api_request": api_request,
        "webhook_deliveries": webhook_deliveries,
    })


@login_required
def request_list(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    qs = ApiRequest.objects.filter(account=account).order_by("-created_at")
    status_code = (request.GET.get("status_code") or "").strip()
    path_q = (request.GET.get("path") or "").strip()
    if status_code.isdigit():
        qs = qs.filter(status_code=int(status_code))
    if path_q:
        qs = qs.filter(path__icontains=path_q)

    page = Paginator(qs, _PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "logs/requests.html", {
        "account": account,
        "page": page,
        "status_code": status_code,
        "path": path_q,
    })


@login_required
def request_detail(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        row = ApiRequest.objects.get(account=account, public_id=public_id)
    except ApiRequest.DoesNotExist:
        return render(request, "logs/not_found.html", status=404)
    linked_message = None
    if row.request_id:
        ev = (
            MessageEvent.objects.filter(account=account, request_id=row.request_id)
            .select_related("message")
            .first()
        )
        linked_message = ev.message if ev else None
    return render(request, "logs/request_detail.html", {
        "account": account,
        "row": row,
        "linked_message": linked_message,
    })
