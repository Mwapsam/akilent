"""Agent inbox and ticket detail views."""

from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.utils import get_current_account
from apps.core.htmx import is_background
from apps.support.models import (
    SupportMessage,
    SupportQueue,
    SupportTicket,
)
from apps.support.services import escalation as esc_service
from apps.support.services import ticket as ticket_service

logger = logging.getLogger(__name__)

_PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


@login_required
def inbox(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    queue_slug = request.GET.get("queue", "")
    status_filter = request.GET.get("status", "open")
    priority_filter = request.GET.get("priority", "")

    qs = SupportTicket.objects.filter(account=account).select_related(
        "category", "queue", "assigned_agent"
    )

    if status_filter == "open":
        qs = qs.exclude(status__in=[SupportTicket.RESOLVED, SupportTicket.CLOSED])
    elif status_filter == "resolved":
        qs = qs.filter(status=SupportTicket.RESOLVED)
    elif status_filter == "closed":
        qs = qs.filter(status=SupportTicket.CLOSED)

    if queue_slug:
        qs = qs.filter(queue__slug=queue_slug)

    if priority_filter:
        qs = qs.filter(priority=priority_filter)

    if request.GET.get("mine"):
        qs = qs.filter(assigned_agent=request.user)

    qs = qs.order_by("priority", "sla_due_at", "-created_at")

    queues = (
        SupportQueue.objects.filter(is_active=True)
        .annotate(
            open_count=Count(
                "tickets",
                filter=Q(
                    tickets__account=account,
                    tickets__status__in=[
                        SupportTicket.NEW,
                        SupportTicket.TRIAGED,
                        SupportTicket.ASSIGNED,
                        SupportTicket.IN_PROGRESS,
                        SupportTicket.WAITING_CUSTOMER,
                        SupportTicket.WAITING_INTERNAL,
                        SupportTicket.ESCALATED,
                        SupportTicket.REOPENED,
                    ],
                ),
            )
        )
        .order_by("sort_order")
    )

    page = Paginator(qs, _PAGE_SIZE).get_page(request.GET.get("page"))

    context = {
        "account": account,
        "page": page,
        "queues": queues,
        "active_queue": queue_slug,
        "status_filter": status_filter,
        "priority_filter": priority_filter,
    }

    if is_background(request):
        return render(request, "support/_inbox_body.html", context)
    return render(request, "support/inbox.html", context)


# ---------------------------------------------------------------------------
# Ticket detail
# ---------------------------------------------------------------------------


@login_required
def ticket_detail(request, ticket_number: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    ticket = get_object_or_404(
        SupportTicket, account=account, ticket_number=ticket_number
    )

    if request.method == "POST":
        return _handle_ticket_action(request, ticket, account)

    messages_qs = (
        SupportMessage.objects.filter(ticket=ticket)
        .select_related("author")
        .order_by("created_at")
    )
    internal_notes = ticket.internal_notes.select_related("author").order_by(
        "created_at"
    )
    events = ticket.events.select_related("actor").order_by("created_at")
    references = ticket.references.select_related("content_type").order_by("created_at")

    from django.contrib.auth.models import User

    agents = User.objects.filter(
        memberships__account=account,
        is_active=True,
    ).order_by("first_name", "last_name")

    context = {
        "account": account,
        "ticket": ticket,
        "messages": messages_qs,
        "internal_notes": internal_notes,
        "events": events,
        "references": references,
        "agents": agents,
        "status_choices": SupportTicket.STATUS_CHOICES,
        "priority_choices": [
            ("p1", "P1 — Critical"),
            ("p2", "P2 — High"),
            ("p3", "P3 — Normal"),
            ("p4", "P4 — Low"),
        ],
    }

    if (
        is_background(request)
        and request.headers.get("HX-Target") == "ticket-pane-inner"
    ):
        return render(request, "support/_ticket_pane.html", context)
    return render(request, "support/ticket_detail.html", context)


def _handle_ticket_action(request, ticket, account):
    action = request.POST.get("action")

    try:
        if action == "reply":
            body = request.POST.get("body", "").strip()
            if body:
                ticket_service.add_message(
                    ticket=ticket, body=body, author=request.user
                )

        elif action == "internal_note":
            body = request.POST.get("body", "").strip()
            if body:
                ticket_service.add_internal_note(
                    ticket=ticket, body=body, author=request.user
                )

        elif action == "change_status":
            new_status = request.POST.get("status")
            if new_status:
                ticket_service.update_status(
                    ticket=ticket, new_status=new_status, actor=request.user
                )

        elif action == "change_priority":
            new_priority = request.POST.get("priority")
            if new_priority:
                ticket_service.set_priority(
                    ticket=ticket, new_priority=new_priority, actor=request.user
                )

        elif action == "escalate":
            esc_service.escalate(
                ticket=ticket,
                to_level=request.POST.get("to_level", "l2"),
                reason="manual",
                actor=request.user,
            )

        elif action == "assign":
            from django.contrib.auth.models import User

            agent_id = request.POST.get("agent_id")
            if agent_id:
                agent = User.objects.filter(pk=agent_id).first()
                ticket_service.assign_agent(
                    ticket=ticket, agent=agent, actor=request.user
                )

    except ValueError as exc:
        logger.warning("ticket_detail action error: %s", exc)

    return redirect("support:detail", ticket_number=ticket.ticket_number)
