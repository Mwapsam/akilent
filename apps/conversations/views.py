"""Inbox: the operational-center UI, built as a projection over the Phase 1
spine (Conversation/Message/Event/Action Registry) — never a parallel state
store. See docs/plans — "UI/UX Principle: Complex Architecture, Simple Product".
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.accounts.utils import get_current_account
from apps.conversations.actions import ActionError, run_action
from apps.conversations.models import Conversation
from apps.conversations.state import (
    ConversationState,
    get_conversation_state,
    missed,
    needs_attention,
    snapshot_of,
    with_activity,
)

_PAGE_SIZE = 30


@login_required
def inbox(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    now = timezone.now()
    view = (request.GET.get("view") or "needs_attention").strip()
    if view == "needs_reply":  # legacy name for the same tab
        view = "needs_attention"

    # Every list is derived from Message rows via conversations.state - never MessageLog.
    if view == "missed":
        qs = missed(account, now)
    elif view == "assigned":
        qs = with_activity(Conversation.objects.filter(account=account, assigned_to=request.user))
    elif view == "all":
        qs = with_activity(Conversation.objects.filter(account=account))
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

    return render(request, "conversations/inbox.html", {
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
    })


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
            messages.error(request, str(exc))
        return redirect("conversations:detail", public_id=public_id)

    conversation.mark_read()
    return render(request, "conversations/conversation_detail.html", {
        "account": account,
        "now": timezone.now(),
        "conversation": conversation,
        "thread": conversation.messages.all().select_related(),
        "snapshot": get_conversation_state(conversation),
        "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
        "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
        "notes": conversation.notes.select_related("author"),
    })
