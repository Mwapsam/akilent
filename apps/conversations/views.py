"""Inbox: the operational-center UI, built as a projection over the Phase 1
spine (Conversation/Message/Event/Action Registry) — never a parallel state
store. See docs/plans — "UI/UX Principle: Complex Architecture, Simple Product".
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.utils import get_current_account
from apps.conversations.actions import ActionError, run_action
from apps.conversations.models import Conversation

_PAGE_SIZE = 30


@login_required
def inbox(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    qs = Conversation.objects.filter(account=account).select_related("contact")
    view = (request.GET.get("view") or "needs_reply").strip()
    if view == "assigned":
        qs = qs.filter(assigned_to=request.user)
    elif view == "all":
        pass
    else:
        view = "needs_reply"
        qs = qs.filter(status=Conversation.Status.OPEN, is_unread=True)

    channel = (request.GET.get("channel") or "").strip()
    if channel:
        qs = qs.filter(channel=channel)

    page = Paginator(qs, _PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "conversations/inbox.html", {
        "account": account,
        "page": page,
        "view": view,
        "channel": channel,
        "channel_choices": Conversation.Channel.choices,
        "needs_reply_count": Conversation.objects.filter(
            account=account, status=Conversation.Status.OPEN, is_unread=True
        ).count(),
        "assigned_count": Conversation.objects.filter(
            account=account, assigned_to=request.user
        ).count(),
        "all_count": Conversation.objects.filter(account=account).count(),
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
        except ActionError as exc:
            messages.error(request, str(exc))
        return redirect("conversations:detail", public_id=public_id)

    conversation.mark_read()
    return render(request, "conversations/conversation_detail.html", {
        "account": account,
        "conversation": conversation,
        "thread": conversation.messages.all().select_related(),
        "notes": conversation.notes.select_related("author"),
    })
