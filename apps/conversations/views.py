"""Inbox: the operational-center UI, built as a projection over the Phase 1
spine (Conversation/Message/Event/Action Registry) — never a parallel state
store. See docs/plans — "UI/UX Principle: Complex Architecture, Simple Product".
"""
from __future__ import annotations

import hashlib
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.conversations.actions import ActionError, run_action
from apps.conversations.models import Conversation, FollowUp, Message, SavedReply
from apps.conversations.state import (
    ConversationState,
    get_conversation_state,
    missed,
    needs_attention,
    snapshot_of,
    with_activity,
)

logger = logging.getLogger(__name__)

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
        # Missed is a kind of waiting that has gone on too long, so both show
        # how long the customer has been waiting rather than the last activity.
        c.is_waiting = bool(
            c.snapshot.missed or c.snapshot.state == ConversationState.WAITING_FOR_AGENT
        ) and c.snapshot.last_customer_message_at is not None
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
    """Rendered inbox body (tabs + list), polled so new conversations appear live.

    HTMX gets the fragment itself, plus a digest of it in ``X-Inbox-Hash``. The
    poller sends the digest it is currently showing back as ``?h=``; when they
    match we answer 204, which HTMX treats as "swap nothing". That keeps the
    original hand-rolled loop's most important property - an unchanged list is
    never re-written into the DOM, so a keyboard user does not lose focus and
    the list does not flicker every 8 seconds.

    Non-HTMX callers keep the original ``{"html": ...}`` JSON shape.
    """
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)

    html = render_to_string(
        "conversations/_inbox_body.html", _inbox_context(request, account), request=request
    )

    if request.headers.get("HX-Request"):
        digest = hashlib.sha1(html.encode("utf-8")).hexdigest()[:12]
        if request.GET.get("h") == digest:
            return HttpResponse(status=204)
        response = HttpResponse(html)
        response["X-Inbox-Hash"] = digest
        return response

    return JsonResponse({"html": html})


def _is_ajax(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _window_is_open(conversation) -> bool:
    """Whether a free-text reply is currently allowed (R0: composer warning).

    Only WhatsApp has a 24h customer-service window today; a channel without
    one is reported open so its composer is unaffected.
    """
    if conversation.channel != Conversation.Channel.WHATSAPP:
        return True
    wa = conversation.whatsapp_conversation
    return bool(wa and wa.window_is_open)


def _automate_offers(account, messages) -> dict:
    """Replies here the team keeps sending by hand: offer to turn them into an automation."""
    from apps.automation import api as automation_api

    ids = [m.id for m in messages if m.direction == Message.Direction.OUTBOUND]
    if not ids:
        return {}
    try:
        return automation_api.automate_offers(account, ids)
    except Exception:  # noqa: BLE001 - an offer must never break the inbox
        logger.exception("automate offers failed")
        return {}


def _ai_config(account, conversation) -> dict:
    """What the composer needs to show AI proposals. ``enabled`` is False whenever AI is off."""
    from apps.ai import api as ai_api

    if not ai_api.is_available(account):
        return {"enabled": False}
    return {
        "enabled": True,
        "suggestUrl": reverse("conversations:ai_suggest", args=[conversation.public_id]),
        "dismissUrl": reverse("conversations:ai_dismiss", args=[conversation.public_id]),
        "applyUrl": reverse("conversations:ai_apply", args=[conversation.public_id]),
        "proposal": _ai_proposal_json(account, conversation),
    }


def _ai_proposal_json(account, conversation):
    try:
        from apps.ai import api as ai_api

        if not ai_api.is_available(account):
            return None
        return ai_api.serialize(ai_api.current_proposal(account, conversation), conversation)
    except Exception:  # noqa: BLE001 - AI trouble must never break the inbox
        logger.exception("could not load the AI proposal for conversation=%s", conversation.pk)
        return None


def _record_ai_use(account, request, sent_text: str) -> None:
    proposal_id = request.POST.get("ai_proposal")
    if not proposal_id:
        return
    try:
        from apps.ai import api as ai_api

        ai_api.record_used(account, proposal_id, sent_text)
    except Exception:  # noqa: BLE001
        logger.exception("could not record AI proposal use")


@login_required
@require_POST
def ai_suggest(request, public_id: str):
    """A person asked AI for a suggestion. It is drafted in the background; the feed delivers it."""
    from apps.ai import api as ai_api

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"ok": False, "error": "no account"}, status=403)
    conversation = get_object_or_404(Conversation, account=account, public_id=public_id)
    proposal = ai_api.request_proposal(account, conversation, request.user)
    if proposal is None:
        return JsonResponse({"ok": False, "error": "AI suggestions aren't switched on."}, status=400)
    return JsonResponse({"ok": True, "proposal": ai_api.serialize(proposal, conversation)})


@login_required
@require_POST
def ai_dismiss(request, public_id: str):
    from apps.ai import api as ai_api

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"ok": False, "error": "no account"}, status=403)
    get_object_or_404(Conversation, account=account, public_id=public_id)
    ai_api.dismiss(account, request.POST.get("proposal"))
    return JsonResponse({"ok": True})


@login_required
@require_POST
def ai_apply(request, public_id: str):
    """A person clicked one of an AI proposal's one-click extras (tag, track interest, follow-up)."""
    from apps.ai import api as ai_api

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"ok": False, "error": "no account"}, status=403)
    conversation = get_object_or_404(Conversation, account=account, public_id=public_id)
    ok, message = ai_api.apply_extra(
        account, conversation, request.POST.get("proposal"), request.POST.get("index"), request.user)
    if not ok:
        return JsonResponse({"ok": False, "error": message}, status=400)
    return JsonResponse({"ok": True, "message": message,
                         "proposal": ai_api.serialize(ai_api.current_proposal(account, conversation), conversation)})


def _team_members(account):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(memberships__account=account).order_by("first_name", "username")


def _resolve_assignee(account, request):
    """Who an "assign" POST means: the requester (default), a teammate, or nobody ("none")."""
    raw = (request.POST.get("assignee") or "").strip()
    if not raw:
        return request.user
    if raw == "none":
        return None
    user = _team_members(account).filter(pk=raw).first() if raw.isdigit() else None
    if user is None:
        raise ActionError("That person isn't on your team.")
    return user


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
                _record_ai_use(account, request, request.POST.get("body", ""))
            elif action == "send_template":
                # The composer's answer to "outside the 24h window" (R1.5c
                # follow-up): send an approved WhatsApp template instead of a
                # plain reply, without leaving the conversation.
                template_id = request.POST.get("template_id")
                if not template_id:
                    raise ActionError("Choose a template.")
                from apps.whatsapp.models import MessageTemplate

                template = MessageTemplate.objects.filter(
                    account=account, pk=template_id, approval_status=MessageTemplate.ApprovalStatus.APPROVED,
                ).first()
                if template is None:
                    raise ActionError("That template isn't available.")
                # Field names are namespaced per template (var__<template_id>__<var>) —
                # several templates can declare the same variable name, and only one
                # <details> section is visible at a time but all its sibling inputs
                # are still submitted (HTML "hidden" doesn't exclude them from the
                # POST), so an unqualified name could silently read another
                # template's stale value.
                params = {
                    var: request.POST.get(f"var__{template.pk}__{var}", "")
                    for var in (template.variables or [])
                }
                run_action(
                    "send_whatsapp", ctx, account=account,
                    phone=conversation.contact.phone, template_id=template.id, params=params,
                    conversation=conversation,
                )
                conversation.mark_read()
                _record_ai_use(account, request, "")
            elif action == "create_followup":
                from apps.conversations.followups import resolve_due_at

                choice = request.POST.get("when", "").strip()
                try:
                    due_at = resolve_due_at(choice, request.POST.get("custom_due_at", ""))
                except ValueError as exc:
                    raise ActionError(str(exc)) from exc
                run_action(
                    "create_followup", ctx, conversation=conversation, due_at=due_at,
                    note=request.POST.get("note", "").strip(), created_by=request.user,
                )
                messages.success(request, "Follow-up scheduled.")
            elif action == "assign":
                run_action(
                    "assign_conversation", ctx, conversation=conversation,
                    user=_resolve_assignee(account, request),
                )
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
    offers = _automate_offers(account, thread)
    chat_config = {
        "feedUrl": reverse("conversations:messages_feed", args=[conversation.public_id]),
        "lastId": thread[-1].id if thread else 0,
        "open": conversation.status == Conversation.Status.OPEN,
        # Only WhatsApp enforces a messaging window today; other channels report
        # it open so the composer behaves as it always has for them.
        "windowOpen": _window_is_open(conversation),
        "ai": _ai_config(account, conversation),
        "messages": [
            {"id": m.id, "direction": m.direction, "body": m.body,
             "ts": m.timestamp.isoformat(), "status": m.status,
             "failureReason": (m.metadata or {}).get("failure_reason") or None,
             "byAi": (m.metadata or {}).get("sent_by") == "ai", "automate": offers.get(m.id)}
            for m in thread
        ],
        "statusHtml": render_to_string("conversations/_status_badges.html", {
            "conversation": conversation, "snapshot": snapshot, "now": now,
            "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
            "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
        }),
    }
    approved_templates = []
    if conversation.channel == Conversation.Channel.WHATSAPP:
        from apps.whatsapp.models import MessageTemplate

        approved_templates = list(
            MessageTemplate.objects.filter(
                account=account, approval_status=MessageTemplate.ApprovalStatus.APPROVED,
            ).order_by("name")
        )

    saved_replies = list(SavedReply.objects.filter(account=account).order_by("title"))
    open_followup = FollowUp.objects.filter(
        account=account, contact=conversation.contact, done_at__isnull=True
    ).order_by("due_at").first()

    open_lead = open_deal = None
    try:
        from apps.crm.models import Deal, Lead

        open_lead = Lead.objects.filter(
            account=account, contact=conversation.contact,
            status__in=[Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED],
        ).first()
        # Giving a lead a value turns it straight into a deal, which leaves no open lead.
        # A customer in the pipeline is still being tracked, so the panel must say so.
        open_deal = Deal.objects.filter(
            account=account, contact=conversation.contact, status=Deal.Status.OPEN,
        ).select_related("stage").order_by("-created_at").first()
    except Exception:
        pass  # crm app not installed/migrated — panel just won't show it

    try:
        from apps.automation.api import activity_for_contact

        automation_activity = activity_for_contact(account, conversation.contact)
    except Exception:
        automation_activity = []  # never let a side panel cost the owner their conversation

    return render(request, "conversations/conversation_detail.html", {
        "account": account,
        "now": now,
        "automation_activity": automation_activity,
        "conversation": conversation,
        "chat_config": chat_config,
        "snapshot": snapshot,
        "notes": conversation.notes.select_related("author"),
        "open_lead": open_lead,
        "open_deal": open_deal,
        "approved_templates": approved_templates,
        "saved_replies": saved_replies,
        "open_followup": open_followup,
        "team_members": _team_members(account),
    })


@login_required
def followups_due(request):
    """"Due today" list (R2.2) — every open follow-up due now or earlier."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    now = timezone.now()
    due = (
        FollowUp.objects.filter(account=account, done_at__isnull=True, due_at__lte=now)
        .select_related("contact", "conversation")
        .order_by("due_at")
    )
    upcoming = (
        FollowUp.objects.filter(account=account, done_at__isnull=True, due_at__gt=now)
        .select_related("contact", "conversation")
        .order_by("due_at")[:20]
    )
    return render(request, "conversations/followups_due.html", {
        "account": account, "now": now, "due": due, "upcoming": upcoming,
    })


@login_required
@require_POST
def followup_complete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    followup = get_object_or_404(FollowUp, account=account, pk=pk)
    run_action("complete_followup", {"account": account}, followup=followup)
    messages.success(request, "Follow-up marked done.")
    return redirect(request.POST.get("next") or "conversations:followups_due")


@login_required
def saved_replies(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        body = request.POST.get("body", "").strip()
        if not title or not body:
            messages.error(request, "A saved reply needs both a title and a message.")
        else:
            SavedReply.objects.create(account=account, title=title, body=body)
            messages.success(request, "Saved reply added.")
        return redirect("conversations:saved_replies")
    replies = SavedReply.objects.filter(account=account).order_by("title")
    return render(request, "conversations/saved_replies.html", {"account": account, "replies": replies})


@login_required
@require_POST
def saved_reply_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    reply = get_object_or_404(SavedReply, account=account, pk=pk)
    reply.delete()
    messages.success(request, "Saved reply removed.")
    return redirect("conversations:saved_replies")


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
    ).order_by("-id").only("id", "status", "metadata")[:20]

    return JsonResponse({
        "messages": [
            {
                "id": m.id,
                "direction": m.direction,
                "body": m.body,
                "timestamp": m.timestamp.isoformat(),
                "status": m.status,
                "failureReason": (m.metadata or {}).get("failure_reason") or None,
                "byAi": (m.metadata or {}).get("sent_by") == "ai",
            }
            for m in new
        ],
        "statuses": {str(m.id): m.status for m in recent_outbound},
        "failureReasons": {
            str(m.id): (m.metadata or {}).get("failure_reason")
            for m in recent_outbound if (m.metadata or {}).get("failure_reason")
        },
        "windowOpen": _window_is_open(conversation),
        "aiProposal": _ai_proposal_json(account, conversation),
        "status_html": render_to_string("conversations/_status_badges.html", {
            "conversation": conversation,
            "snapshot": get_conversation_state(conversation),
            "now": timezone.now(),
            "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
            "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
        }),
        "open": conversation.status == Conversation.Status.OPEN,
    })
