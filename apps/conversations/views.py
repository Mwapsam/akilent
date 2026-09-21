"""Inbox: the operational-center UI, built as a projection over the Phase 1
spine (Conversation/Message/Event/Action Registry) — never a parallel state
store. See docs/plans — "UI/UX Principle: Complex Architecture, Simple Product".
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.accounts.utils import get_current_account
from apps.conversations.actions import ActionError, run_action
from apps.conversations.models import Conversation, Message
from apps.conversations.state import (
    ConversationState,
    get_conversation_state,
    missed,
    needs_attention,
    snapshot_of,
    with_activity,
)

_PAGE_SIZE = 30


def _inbox_context(request, account) -> dict:
    now = timezone.now()
    view = (request.GET.get("view") or "needs_attention").strip()
    if view == "needs_reply":  # legacy name for the same tab
        view = "needs_attention"

    # Every list is derived from Message rows via conversations.state - never MessageLog.
    if view == "missed":
        qs = missed(account, now)
    elif view == "assigned":
        qs = with_activity(
            Conversation.objects.filter(account=account, assigned_to=request.user)
        ).order_by("-last_any", "-id")
    elif view == "all":
        qs = with_activity(Conversation.objects.filter(account=account)).order_by("-last_any", "-id")
    else:
        view = "needs_attention"
        qs = needs_attention(account, now)
    qs = qs.select_related("contact")

    channel = (request.GET.get("channel") or "").strip()
    if channel:
        qs = qs.filter(channel=channel)

    page = Paginator(qs, _PAGE_SIZE).get_page(request.GET.get("page"))
    for c in page:
        c.snapshot = snapshot_of(c, now)
        last = c.messages.order_by("-timestamp", "-id").only("body", "direction").first()
        c.preview = last

    return {
        "account": account,
        "now": now,
        "page": page,
        "view": view,
        "channel": channel,
        "channel_choices": Conversation.Channel.choices,
        "needs_attention_count": needs_attention(account, now).count(),
        "missed_count": missed(account, now).count(),
        "assigned_count": Conversation.objects.filter(
            account=account, assigned_to=request.user
        ).count(),
        "all_count": Conversation.objects.filter(account=account).count(),
        "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
        "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
    }


@login_required
def inbox(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    return render(request, "conversations/inbox.html", _inbox_context(request, account))


@login_required
def inbox_feed(request):
    """Rendered inbox body (tabs + list), polled so new conversations appear live."""
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)
    html = render_to_string(
        "conversations/_inbox_body.html", _inbox_context(request, account), request=request
    )
    return JsonResponse({"html": html})


def _is_ajax(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


@login_required
def conversation_detail(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    conversation = get_object_or_404(Conversation, account=account, public_id=public_id)

    if request.method == "POST":
        action = request.POST.get("action")
        ctx = {"account": account}
        try:
            if action == "reply":
                run_action("reply", ctx, conversation=conversation, body=request.POST.get("body", "").strip())
                conversation.mark_read()
            elif action == "assign":
                run_action("assign_conversation", ctx, conversation=conversation, user=request.user)
            elif action == "add_note":
                run_action(
                    "add_internal_note", ctx, conversation=conversation,
                    body=request.POST.get("body", "").strip(), author=request.user,
                )
            elif action == "mark_read":
                conversation.mark_read()
            elif action == "close":
                conversation.close()
        except ActionError as exc:
            if _is_ajax(request):
                return JsonResponse({"ok": False, "error": str(exc)}, status=400)
            messages.error(request, str(exc))
        else:
            if _is_ajax(request):
                return JsonResponse({"ok": True})
        return redirect("conversations:detail", public_id=public_id)

    conversation.mark_read()
    thread = list(conversation.messages.all().order_by("timestamp", "id"))
    snapshot = get_conversation_state(conversation)
    now = timezone.now()
    chat_config = {
        "feedUrl": reverse("conversations:messages_feed", args=[conversation.public_id]),
        "lastId": thread[-1].id if thread else 0,
        "open": conversation.status == Conversation.Status.OPEN,
        "messages": [
            {"id": m.id, "direction": m.direction, "body": m.body,
             "ts": m.timestamp.isoformat(), "status": m.status}
            for m in thread
        ],
        "statusHtml": render_to_string("conversations/_status_badges.html", {
            "conversation": conversation, "snapshot": snapshot, "now": now,
            "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
            "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
        }),
    }
    return render(request, "conversations/conversation_detail.html", {
        "account": account,
        "now": now,
        "conversation": conversation,
        "chat_config": chat_config,
        "snapshot": snapshot,
        "notes": conversation.notes.select_related("author"),
    })


@login_required
def messages_feed(request, public_id: str):
    """Messages newer than ``?after=<id>`` plus current delivery statuses and state badges."""
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)
    conversation = get_object_or_404(Conversation, account=account, public_id=public_id)

    try:
        after = max(int(request.GET.get("after") or 0), 0)
    except ValueError:
        after = 0

    new = list(conversation.messages.filter(id__gt=after).order_by("id")[:100])
    if any(m.direction == Message.Direction.INBOUND for m in new):
        conversation.mark_read()
        conversation.refresh_from_db(fields=["status"])

    recent_outbound = conversation.messages.filter(
        direction=Message.Direction.OUTBOUND
    ).order_by("-id").values_list("id", "status")[:20]

    return JsonResponse({
        "messages": [
            {
                "id": m.id,
                "direction": m.direction,
                "body": m.body,
                "timestamp": m.timestamp.isoformat(),
                "status": m.status,
            }
            for m in new
        ],
        "statuses": {str(pk): st for pk, st in recent_outbound},
        "status_html": render_to_string("conversations/_status_badges.html", {
            "conversation": conversation,
            "snapshot": get_conversation_state(conversation),
            "now": timezone.now(),
            "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
            "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
        }),
        "open": conversation.status == Conversation.Status.OPEN,
    })
