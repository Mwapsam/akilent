"""Dashboard: Email Logs + Request logs (Phase 2 / Epic P0.2)."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render

from apps.accounts.utils import get_current_account
from apps.core.htmx import is_background
from apps.email.models import EmailMessage, WebhookDelivery
from apps.logs.models import ApiRequest, BrowserSession, MessageEvent, UxEvent

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
    total_ever = (
        qs.model.objects.filter(account=account).exists() if (q or st) else qs.exists()
    )
    context = {
        "account": account,
        "page": page,
        "q": q,
        "status": st,
        "status_choices": _STATUS_CHOICES,
        "show_quickstart": not total_ever,
    }
    # HTMX asks for the results region on its own; a normal navigation gets
    # the whole page. Same view, same context.
    if is_background(request):
        return render(request, "logs/_messages_results.html", context)
    return render(request, "logs/messages.html", context)


@login_required
def message_detail(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    try:
        msg = EmailMessage.objects.select_related("template", "domain", "campaign").get(
            account=account, public_id=public_id
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
    return render(
        request,
        "logs/message_detail.html",
        {
            "account": account,
            "msg": msg,
            "events": events,
            "api_request": api_request,
            "webhook_deliveries": webhook_deliveries,
        },
    )


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
    context = {
        "account": account,
        "page": page,
        "status_code": status_code,
        "path": path_q,
    }
    if is_background(request):
        return render(request, "logs/_requests_results.html", context)
    return render(request, "logs/requests.html", context)


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

    # Preceding 30-second session timeline (inline — no separate page visit needed)
    preceding_timeline: list[dict] = []
    if row.browser_session_id:
        from datetime import timedelta

        window_start = row.created_at - timedelta(seconds=30)
        prior_requests = list(
            ApiRequest.objects.filter(
                browser_session_id=row.browser_session_id,
                created_at__range=(window_start, row.created_at),
            )
            .exclude(pk=row.pk)
            .values(
                "created_at", "method", "path", "status_code", "request_id", "public_id"
            )
        )
        ux_events = list(
            UxEvent.objects.filter(
                session__session_id=row.browser_session_id,
                occurred_at__range=(window_start, row.created_at),
            ).values(
                "occurred_at", "type", "page", "target", "click_count", "request_id"
            )
        )
        for r in prior_requests:
            preceding_timeline.append(
                {"ts": r["created_at"], "kind": "request", "obj": r}
            )
        for e in ux_events:
            preceding_timeline.append({"ts": e["occurred_at"], "kind": "ux", "obj": e})
        preceding_timeline.sort(key=lambda x: x["ts"])  # type: ignore[arg-type]

    session_obj = None
    if row.browser_session_id:
        session_obj = BrowserSession.objects.filter(
            session_id=row.browser_session_id
        ).first()

    return render(
        request,
        "logs/request_detail.html",
        {
            "account": account,
            "row": row,
            "linked_message": linked_message,
            "preceding_timeline": preceding_timeline,
            "session_obj": session_obj,
        },
    )
