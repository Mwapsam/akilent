"""Inbox: the operational-center UI, built as a projection over the Phase 1
spine (Conversation/Message/Event/Action Registry) — never a parallel state
store. See docs/plans — "UI/UX Principle: Complex Architecture, Simple Product".
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.models import Team
from apps.accounts.utils import get_current_account, viewing_as
from apps.conversations.actions import ActionError, run_action
from apps.conversations.api import conversation_window_is_open
from apps.conversations.media import media_json
from apps.conversations.models import (
    Conversation,
    ConversationForm,
    FollowUp,
    Message,
    RoutingRule,
    SavedReply,
)
from apps.conversations.services import delete_conversation
from apps.conversations.state import (
    OVERDUE_WAITING,
    ConversationState,
    get_conversation_state,
    missed,
    needs_attention,
    snapshot_of,
    unanswered_q,
    with_activity,
)
from apps.core.htmx import is_background

if TYPE_CHECKING:
    from django.http.response import HttpResponseBase

logger = logging.getLogger(__name__)

_PAGE_SIZE = 30


def _inbox_context(request, account) -> dict:
    now = timezone.now()
    view = (request.GET.get("view") or "all").strip()
    if view == "needs_reply":  # legacy name for the same tab
        view = "needs_attention"

    # Every list is derived from Message rows via conversations.state - never MessageLog.
    if view == "missed":
        qs = missed(account, now)
    elif view == "assigned":
        qs = with_activity(
            Conversation.objects.filter(account=account, assigned_to=request.user)
        ).order_by("-last_any", "-id")
    elif view == "unassigned":
        # Nobody is responsible for this conversation yet — an open queue
        # waiting to be routed, longest-waiting first like needs_attention.
        qs = with_activity(
            Conversation.objects.filter(
                account=account,
                status=Conversation.Status.OPEN,
                assigned_to__isnull=True,
            )
        ).order_by("last_in")
    elif view == "closed":
        qs = with_activity(
            Conversation.objects.filter(
                account=account, status=Conversation.Status.CLOSED
            )
        ).order_by("-last_any", "-id")
    elif view == "needs_attention":
        qs = needs_attention(account, now)
    else:
        view = "all"
        qs = with_activity(Conversation.objects.filter(account=account)).order_by(
            "-last_any", "-id"
        )
    qs = qs.select_related("contact")

    channel = (request.GET.get("channel") or "").strip()
    if channel:
        qs = qs.filter(channel=channel)

    # Combinable with any tab ("My + Overdue", "Unassigned + Overdue"): the
    # same waiting-on-us definition every tab already uses, just past the
    # stricter OVERDUE_WAITING cutoff instead of the 24h one.
    overdue_only = bool(request.GET.get("overdue"))
    if overdue_only:
        qs = qs.filter(unanswered_q(), last_in__lte=now - OVERDUE_WAITING)

    page = Paginator(qs, _PAGE_SIZE).get_page(request.GET.get("page"))
    for c in page:
        c.snapshot = snapshot_of(c, now)
        # Missed is a kind of waiting that has gone on too long, so both show
        # how long the customer has been waiting rather than the last activity.
        c.is_waiting = (
            bool(
                c.snapshot.missed
                or c.snapshot.state == ConversationState.WAITING_FOR_AGENT
            )
            and c.snapshot.last_customer_message_at is not None
        )
        last = (
            c.messages.order_by("-timestamp", "-id").only("body", "direction").first()
        )
        c.preview = last

    return {
        "account": account,
        "now": now,
        "page": page,
        "view": view,
        "channel": channel,
        "channel_choices": Conversation.Channel.choices,
        "overdue_only": overdue_only,
        "needs_attention_count": needs_attention(account, now).count(),
        "missed_count": missed(account, now).count(),
        "assigned_count": Conversation.objects.filter(
            account=account, assigned_to=request.user
        ).count(),
        "unassigned_count": Conversation.objects.filter(
            account=account,
            status=Conversation.Status.OPEN,
            assigned_to__isnull=True,
        ).count(),
        "closed_count": Conversation.objects.filter(
            account=account, status=Conversation.Status.CLOSED
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
        "conversations/_inbox_body.html",
        _inbox_context(request, account),
        request=request,
    )

    if is_background(request):
        digest = hashlib.sha1(html.encode("utf-8"), usedforsecurity=False).hexdigest()[
            :12
        ]
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

    WhatsApp and Instagram both allow free text for 24h after the customer's last
    message; a channel without a window is reported open so its composer is
    unaffected.
    """
    return conversation_window_is_open(conversation)


def _automate_offers(account, messages) -> dict:
    """Replies here the team keeps sending by hand: offer to turn them into an automation."""
    from apps.automation import api as automation_api

    ids = [m.id for m in messages if m.direction == Message.Direction.OUTBOUND]
    if not ids:
        return {}
    try:
        return automation_api.automate_offers(account, ids)
    except Exception:
        logger.exception("automate offers failed")
        return {}


def _ai_config(account, conversation) -> dict:
    """What the composer needs to show AI proposals. ``enabled`` is False whenever AI is off."""
    from apps.ai import api as ai_api

    if not ai_api.is_available(account):
        return {"enabled": False}
    return {
        "enabled": True,
        "suggestUrl": reverse(
            "conversations:ai_suggest", args=[conversation.public_id]
        ),
        "dismissUrl": reverse(
            "conversations:ai_dismiss", args=[conversation.public_id]
        ),
        "applyUrl": reverse("conversations:ai_apply", args=[conversation.public_id]),
        "proposal": _ai_proposal_json(account, conversation),
    }


def _ai_proposal_json(account, conversation):
    try:
        from apps.ai import api as ai_api

        if not ai_api.is_available(account):
            return None
        return ai_api.serialize(
            ai_api.current_proposal(account, conversation), conversation
        )
    except Exception:
        logger.exception(
            "could not load the AI proposal for conversation=%s", conversation.pk
        )
        return None


def _record_ai_use(account, request, sent_text: str) -> None:
    proposal_id = request.POST.get("ai_proposal")
    if not proposal_id:
        return
    try:
        from apps.ai import api as ai_api

        ai_api.record_used(account, proposal_id, sent_text)
    except Exception:
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
        return JsonResponse(
            {"ok": False, "error": "AI suggestions aren't switched on."}, status=400
        )
    return JsonResponse(
        {"ok": True, "proposal": ai_api.serialize(proposal, conversation)}
    )


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
        account,
        conversation,
        request.POST.get("proposal"),
        request.POST.get("index"),
        request.user,
    )
    if not ok:
        return JsonResponse({"ok": False, "error": message}, status=400)
    return JsonResponse(
        {
            "ok": True,
            "message": message,
            "proposal": ai_api.serialize(
                ai_api.current_proposal(account, conversation), conversation
            ),
        }
    )


def _team_members(account):
    from django.contrib.auth import get_user_model

    return (
        get_user_model()
        .objects.filter(memberships__account=account)
        .order_by("first_name", "username")
    )


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
                run_action(
                    "reply",
                    ctx,
                    conversation=conversation,
                    body=request.POST.get("body", "").strip(),
                )
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
                    account=account,
                    pk=template_id,
                    approval_status=MessageTemplate.ApprovalStatus.APPROVED,
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
                    "send_whatsapp",
                    ctx,
                    account=account,
                    phone=conversation.contact.phone,
                    template_id=template.id,
                    params=params,
                    conversation=conversation,
                )
                conversation.mark_read()
                _record_ai_use(account, request, "")
            elif action == "create_followup":
                from apps.conversations.followups import resolve_due_at

                choice = request.POST.get("when", "").strip()
                try:
                    due_at = resolve_due_at(
                        choice, request.POST.get("custom_due_at", "")
                    )
                except ValueError as exc:
                    raise ActionError(str(exc)) from exc
                run_action(
                    "create_followup",
                    ctx,
                    conversation=conversation,
                    due_at=due_at,
                    note=request.POST.get("note", "").strip(),
                    created_by=request.user,
                )
                messages.success(request, "Follow-up scheduled.")
            elif action == "assign":
                run_action(
                    "assign_conversation",
                    ctx,
                    conversation=conversation,
                    user=_resolve_assignee(account, request),
                    assigned_by=request.user,
                )
            elif action == "add_note":
                run_action(
                    "add_internal_note",
                    ctx,
                    conversation=conversation,
                    body=request.POST.get("body", "").strip(),
                    author=request.user,
                )
            elif action == "mark_read":
                conversation.mark_read()
            elif action == "close":
                resolution = request.POST.get("resolution", "")
                if resolution and resolution not in Conversation.Resolution.values:
                    raise ActionError("That's not a valid close reason.")
                conversation.close(
                    resolution=resolution, actor=f"user:{request.user.pk}"
                )
            elif action == "reopen":
                conversation.reopen(actor=f"user:{request.user.pk}")
            elif action == "start_form":
                form = ConversationForm.objects.filter(
                    account=account,
                    pk=request.POST.get("form_id"),
                    status=ConversationForm.Status.PUBLISHED,
                ).first()
                if form is None:
                    raise ActionError("Choose a form to start.")
                run_action(
                    "start_conversation_form", ctx, conversation=conversation, form=form
                )
        except ActionError as exc:
            if _is_ajax(request):
                return JsonResponse({"ok": False, "error": str(exc)}, status=400)
            messages.error(request, str(exc))
        else:
            if _is_ajax(request):
                return JsonResponse({"ok": True})
        return redirect("conversations:detail", public_id=public_id)

    if (
        viewing_as(request) is None
    ):  # support looking "as" the business mustn't clear its unread
        conversation.mark_read()
    thread = list(
        conversation.messages.select_related("whatsapp_message", "instagram_message")
        .all()
        .order_by("timestamp", "id")
    )
    snapshot = get_conversation_state(conversation)
    now = timezone.now()
    offers = _automate_offers(account, thread)
    chat_config = {
        "id": conversation.public_id,  # matches the conversation_id realtime.publish() sends
        "feedUrl": reverse(
            "conversations:messages_feed", args=[conversation.public_id]
        ),
        "sendMediaUrl": reverse(
            "conversations:send_media", args=[conversation.public_id]
        ),
        "lastId": thread[-1].id if thread else 0,
        "open": conversation.status == Conversation.Status.OPEN,
        # Only WhatsApp enforces a messaging window today; other channels report
        # it open so the composer behaves as it always has for them.
        "windowOpen": _window_is_open(conversation),
        "ai": _ai_config(account, conversation),
        "messages": [
            {
                "id": m.id,
                "direction": m.direction,
                "body": m.body,
                "ts": m.timestamp.isoformat(),
                "status": m.status,
                "failureReason": (m.metadata or {}).get("failure_reason") or None,
                "byAi": (m.metadata or {}).get("sent_by") == "ai",
                "automate": offers.get(m.id),
                "media": media_json(m, conversation.public_id),
            }
            for m in thread
        ],
        "statusHtml": render_to_string(
            "conversations/_status_badges.html",
            {
                "conversation": conversation,
                "snapshot": snapshot,
                "now": now,
                "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
                "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
            },
        ),
    }
    approved_templates = []
    if conversation.channel == Conversation.Channel.WHATSAPP:
        from apps.whatsapp.models import MessageTemplate

        approved_templates = list(
            MessageTemplate.objects.filter(
                account=account,
                approval_status=MessageTemplate.ApprovalStatus.APPROVED,
            ).order_by("name")
        )

    saved_replies = list(SavedReply.objects.filter(account=account).order_by("title"))
    open_followup = (
        FollowUp.objects.filter(
            account=account, contact=conversation.contact, done_at__isnull=True
        )
        .order_by("due_at")
        .first()
    )

    open_lead = open_deal = None
    try:
        from apps.crm.models import Deal, Lead

        open_lead = Lead.objects.filter(
            account=account,
            contact=conversation.contact,
            status__in=[Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED],
        ).first()
        # Giving a lead a value turns it straight into a deal, which leaves no open lead.
        # A customer in the pipeline is still being tracked, so the panel must say so.
        open_deal = (
            Deal.objects.filter(
                account=account,
                contact=conversation.contact,
                status=Deal.Status.OPEN,
            )
            .select_related("stage")
            .order_by("-created_at")
            .first()
        )
    except Exception:
        pass  # crm app not installed/migrated — panel just won't show it

    open_order = None
    try:
        from apps.commerce.models import Order

        open_order = (
            Order.objects.filter(
                account=account,
                contact=conversation.contact,
                status__in=[Order.Status.PENDING, Order.Status.AWAITING_PAYMENT],
            )
            .order_by("-created_at")
            .first()
        )
    except Exception:
        pass  # commerce app not installed/migrated — panel just won't show it

    # Cheap facts a support agent wants without leaving the conversation:
    # how long this has been a customer, and whether this is a first
    # contact or one of a longer history. Computed here, not stored.
    conversation_count = Conversation.objects.filter(
        account=account, contact=conversation.contact
    ).count()

    try:
        from apps.automation.api import activity_for_contact

        automation_activity = activity_for_contact(account, conversation.contact)
    except Exception:
        automation_activity = []  # never let a side panel cost the owner their conversation

    from apps.conversations import forms as conversation_forms

    active_form_response = conversation_forms.active_response_for(conversation)
    published_forms = (
        ConversationForm.objects.filter(
            account=account, status=ConversationForm.Status.PUBLISHED
        ).order_by("name")
        if active_form_response is None
        else ConversationForm.objects.none()
    )

    active_recommendation = _active_recommendation_for(conversation)

    pane_context = {
        "account": account,
        "now": now,
        "automation_activity": automation_activity,
        "conversation": conversation,
        "active_recommendation": active_recommendation,
        "chat_config": chat_config,
        "snapshot": snapshot,
        "notes": conversation.notes.select_related("author"),
        "open_lead": open_lead,
        "open_deal": open_deal,
        "open_order": open_order,
        "conversation_count": conversation_count,
        "approved_templates": approved_templates,
        "saved_replies": saved_replies,
        "open_followup": open_followup,
        "team_members": _team_members(account),
        "active_form_response": active_form_response,
        "published_forms": published_forms,
        "can_delete": _can_delete(request, account),
    }
    # The inbox list (templates/conversations/inbox.html, ≥lg) loads a conversation into its
    # own pane via a plain background GET — see static/js/inbox_pane.js — rather than a full
    # navigation, so opening one never re-loads the shell or the list beside it. Below lg there
    # is no pane to load into, so a row's own href still points here and gets the full page below.
    if (
        is_background(request)
        and request.headers.get("HX-Target") == "conversation-pane-inner"
    ):
        return render(request, "conversations/_conversation_pane.html", pane_context)

    return render(request, "conversations/conversation_detail.html", pane_context)


def _can_delete(request, account) -> bool:
    """Owners and admins only, and never while an operator is viewing as the business."""
    from apps.accounts import api as accounts_api

    return viewing_as(request) is None and accounts_api.is_account_admin(
        request.user, account
    )


@login_required
@require_POST
def conversation_delete(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    conversation = get_object_or_404(Conversation, account=account, public_id=public_id)
    if not _can_delete(request, account):
        messages.error(request, "Only owners and admins can delete conversations.")
        return redirect("conversations:detail", public_id=public_id)
    delete_conversation(conversation, actor=request.user)
    messages.success(request, "Conversation deleted.")
    return redirect("conversations:inbox")


@login_required
def followups_due(request):
    """ "Due today" list (R2.2) — every open follow-up due now or earlier."""
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
    return render(
        request,
        "conversations/followups_due.html",
        {
            "account": account,
            "now": now,
            "due": due,
            "upcoming": upcoming,
        },
    )


@login_required
@require_POST
def followup_complete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    followup = get_object_or_404(FollowUp, account=account, pk=pk)
    run_action("complete_followup", {"account": account}, followup=followup)
    messages.success(request, "Follow-up marked done.")
    next_url = request.POST.get("next") or ""
    if next_url and not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        next_url = ""
    return redirect(next_url or "conversations:followups_due")


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
    return render(
        request,
        "conversations/saved_replies.html",
        {"account": account, "replies": replies},
    )


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
def teams(request):
    """B.2: organizational teams (Sales, Support, ...) that RoutingRules target.
    Deliberately minimal — a name and a member roster, no per-team settings."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "A team needs a name.")
        elif Team.objects.filter(account=account, name=name).exists():
            messages.error(request, "A team with that name already exists.")
        else:
            Team.objects.create(account=account, name=name)
            messages.success(request, "Team added.")
        return redirect("conversations:teams")

    team_list = (
        Team.objects.filter(account=account)
        .annotate(member_count=Count("members"))
        .order_by("name")
    )
    return render(
        request, "conversations/teams.html", {"account": account, "teams": team_list}
    )


@login_required
def team_detail(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    team = get_object_or_404(Team, account=account, pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")
        raw = request.POST.get("user_id", "")
        user = _team_members(account).filter(pk=raw).first() if raw.isdigit() else None
        if user is None:
            messages.error(request, "That person isn't on your team.")
        elif action == "add_member":
            team.members.add(user)
        elif action == "remove_member":
            team.members.remove(user)
        return redirect("conversations:team_detail", pk=team.pk)

    current_members = team.members.all().order_by("first_name", "username")
    available = _team_members(account).exclude(
        pk__in=current_members.values_list("pk", flat=True)
    )
    return render(
        request,
        "conversations/team_detail.html",
        {
            "account": account,
            "team": team,
            "members": current_members,
            "available": available,
        },
    )


@login_required
def routing_rules(request):
    """B.2: which team a new conversation goes to. Deliberately a flat AND of a
    handful of structured signals (see ``apps.conversations.routing``) — not a
    rule builder — so the form below just checks the boxes that apply."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        team = Team.objects.filter(account=account, pk=request.POST.get("team")).first()
        if not name or team is None:
            messages.error(request, "A routing rule needs a name and a team.")
        else:
            conditions: dict = {}
            channel = request.POST.get("channel", "").strip()
            if channel:
                conditions["channel"] = channel
            tag = request.POST.get("tag", "").strip()
            if tag:
                conditions["tag"] = tag
            for key in ("is_new_customer", "has_open_lead", "has_open_order"):
                if request.POST.get(key):
                    conditions[key] = True
            try:
                priority = int(request.POST.get("priority") or 0)
            except ValueError:
                priority = 0
            RoutingRule.objects.create(
                account=account,
                name=name,
                team=team,
                conditions=conditions,
                priority=priority,
            )
            messages.success(request, "Routing rule added.")
        return redirect("conversations:routing_rules")

    rules = (
        RoutingRule.objects.filter(account=account)
        .select_related("team")
        .order_by("priority", "id")
    )
    return render(
        request,
        "conversations/routing_rules.html",
        {
            "account": account,
            "rules": rules,
            "teams": Team.objects.filter(account=account).order_by("name"),
            "channel_choices": Conversation.Channel.choices,
        },
    )


@login_required
@require_POST
def routing_rule_toggle(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    rule = get_object_or_404(RoutingRule, account=account, pk=pk)
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active"])
    return redirect("conversations:routing_rules")


@login_required
@require_POST
def routing_rule_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    rule = get_object_or_404(RoutingRule, account=account, pk=pk)
    rule.delete()
    messages.success(request, "Routing rule removed.")
    return redirect("conversations:routing_rules")


_FORM_FIELD_TYPES = ["text", "email", "phone", "number"]


@login_required
def forms_list(request):
    """B.3/Phase C: native WhatsApp forms (apps.conversations.forms). Deliberately
    deterministic — no AI drafts or answers a form question."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "A form needs a name.")
            return redirect("conversations:forms_list")
        form = ConversationForm.objects.create(account=account, name=name)
        return redirect("conversations:form_detail", pk=form.pk)
    forms_qs = ConversationForm.objects.filter(account=account).order_by("name")
    return render(
        request,
        "conversations/forms_list.html",
        {"account": account, "forms": forms_qs},
    )


def _maps_to_from_post(request) -> str:
    kind = request.POST.get("maps_to_type", "")
    if kind == "first_name":
        return "contact.first_name"
    if kind == "last_name":
        return "contact.last_name"
    if kind == "custom":
        key = slugify(request.POST.get("maps_to_key", "")).replace("-", "_")
        return f"contact.attributes.{key}" if key else ""
    return ""


@login_required
def form_detail(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    form = get_object_or_404(ConversationForm, account=account, pk=pk)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_question":
            label = request.POST.get("label", "").strip()
            field_type = request.POST.get("field_type", "text")
            if not label:
                messages.error(request, "A question needs a label.")
            elif field_type not in _FORM_FIELD_TYPES:
                messages.error(request, "That's not a valid answer type.")
            else:
                used_keys = {q["key"] for q in form.questions}
                base = slugify(label).replace("-", "_") or "question"
                key, n = base, 2
                while key in used_keys:
                    key, n = f"{base}_{n}", n + 1
                form.questions = [
                    *form.questions,
                    {
                        "key": key,
                        "label": label,
                        "field_type": field_type,
                        "maps_to": _maps_to_from_post(request),
                    },
                ]
                form.save(update_fields=["questions", "updated_at"])
                form.note_questions_changed()
                messages.success(request, "Question added.")
        elif action == "remove_question":
            try:
                index = int(request.POST.get("index", -1))
            except ValueError:
                index = -1
            if 0 <= index < len(form.questions):
                form.questions = [q for i, q in enumerate(form.questions) if i != index]
                form.save(update_fields=["questions", "updated_at"])
                form.note_questions_changed()
        elif action == "set_presentation":
            presentation = request.POST.get("presentation", "")
            if presentation in ConversationForm.Presentation.values:
                form.presentation = presentation
                form.save(update_fields=["presentation", "updated_at"])
        elif action == "publish_flow":
            from apps.whatsapp.tasks import publish_conversation_flow

            publish_conversation_flow(form)
            form.refresh_from_db()
            if form.flow_status == ConversationForm.FlowStatus.PUBLISHED:
                messages.success(request, "WhatsApp Flow published successfully.")
            else:
                messages.error(
                    request, f"Publish failed: {form.flow_error or 'unknown error'}"
                )
        elif action == "set_status":
            status = request.POST.get("status", "")
            if status == ConversationForm.Status.PUBLISHED and not form.questions:
                messages.error(request, "Add at least one question before publishing.")
            elif status in ConversationForm.Status.values:
                form.status = status
                form.save(update_fields=["status", "updated_at"])
        return redirect("conversations:form_detail", pk=form.pk)

    return render(
        request,
        "conversations/form_detail.html",
        {"account": account, "form": form, "field_types": _FORM_FIELD_TYPES},
    )


@login_required
@require_POST
def form_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    form = get_object_or_404(ConversationForm, account=account, pk=pk)
    form.delete()
    messages.success(request, "Form deleted.")
    return redirect("conversations:forms_list")


@login_required
@require_POST
def send_media(request, public_id: str):
    """Send a file or a recorded voice note from the composer. JSON for the inbox script."""
    from apps.conversations import outbound_media

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"ok": False, "error": "No account."}, status=403)
    conversation = get_object_or_404(Conversation, account=account, public_id=public_id)
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse(
            {"ok": False, "error": "Choose a file to send."}, status=400
        )
    channel = conversation.channel
    if channel not in (outbound_media.WHATSAPP, outbound_media.INSTAGRAM):
        return JsonResponse(
            {"ok": False, "error": "Media can't be sent on this channel."}, status=400
        )
    try:
        media = outbound_media.prepare(
            upload,
            channel,
            account_id=account.pk,
            voice=request.POST.get("voice") == "1",
        )
        result = run_action(
            "reply_media",
            {"account": account},
            conversation=conversation,
            media=media,
            caption=(request.POST.get("caption") or "").strip(),
        )
    except (outbound_media.MediaError, ActionError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    conversation.mark_read()
    return JsonResponse({"ok": True, **result})


def _recent_media(conversation) -> dict:
    """``{message_id: media_json}`` for the latest inbound messages that carry media."""
    recent = (
        conversation.messages.filter(direction=Message.Direction.INBOUND)
        .select_related("whatsapp_message", "instagram_message")
        .order_by("-id")[:20]
    )
    out = {}
    for m in recent:
        media = media_json(m, conversation.public_id)
        if media is not None:
            out[str(m.id)] = media
    return out


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """``(start, end)`` for a single ``bytes=`` range within ``size``, else None."""
    if not header.startswith("bytes=") or "," in header or size <= 0:
        return None
    first, _, last = header[6:].strip().partition("-")
    try:
        if first == "":  # suffix range: the last N bytes
            start, end = max(size - int(last), 0), size - 1
        else:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return start, end


@login_required
def message_media(request, public_id: str, message_id: int):
    """Stream one message's photo / video / voice note / file to a signed-in teammate.

    Customer media is private: it is never linked by a public storage URL, only
    served here after checking the conversation belongs to the viewer's business.
    """
    from django.http import FileResponse, Http404

    from apps.conversations.media import stored_media

    account = get_current_account(request)
    if account is None:
        raise Http404
    conversation = get_object_or_404(Conversation, account=account, public_id=public_id)
    message = get_object_or_404(
        Message.objects.select_related("whatsapp_message", "instagram_message"),
        pk=message_id,
        conversation=conversation,
    )
    media = stored_media(message)
    if media is None:
        raise Http404
    try:
        handle = media.file.open("rb")
        size = media.file.size
    except (FileNotFoundError, OSError) as exc:
        raise Http404 from exc
    mime = media.mime or "application/octet-stream"
    playable = mime.split("/", 1)[0] in {"image", "video", "audio"}
    filename = (media.file.name or "file").rsplit("/", 1)[-1]

    # Safari only plays audio/video when the server honours byte ranges.
    response: HttpResponseBase
    byte_range = _parse_range(request.headers.get("Range", ""), size)
    if byte_range is not None:
        start, end = byte_range
        handle.seek(start)
        response = HttpResponse(
            handle.read(end - start + 1), status=206, content_type=mime
        )
        handle.close()
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = str(end - start + 1)
    else:
        response = FileResponse(
            handle, content_type=mime, as_attachment=not playable, filename=filename
        )
    response["Accept-Ranges"] = "bytes"
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, max-age=3600"
    return response


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

    new = list(
        conversation.messages.select_related("whatsapp_message", "instagram_message")
        .filter(id__gt=after)
        .order_by("id")[:100]
    )
    if (
        any(m.direction == Message.Direction.INBOUND for m in new)
        and viewing_as(request) is None
    ):
        conversation.mark_read()
        conversation.refresh_from_db(fields=["status"])

    recent_outbound = (
        conversation.messages.filter(direction=Message.Direction.OUTBOUND)
        .order_by("-id")
        .only("id", "status", "metadata")[:20]
    )

    return JsonResponse(
        {
            "messages": [
                {
                    "id": m.id,
                    "direction": m.direction,
                    "body": m.body,
                    "timestamp": m.timestamp.isoformat(),
                    "status": m.status,
                    "failureReason": (m.metadata or {}).get("failure_reason") or None,
                    "byAi": (m.metadata or {}).get("sent_by") == "ai",
                    "media": media_json(m, conversation.public_id),
                }
                for m in new
            ],
            "statuses": {str(m.id): m.status for m in recent_outbound},
            # Media that was still downloading when first shown: its current state,
            # so a photo or voice note appears without reloading the page.
            "media": _recent_media(conversation),
            "failureReasons": {
                str(m.id): (m.metadata or {}).get("failure_reason")
                for m in recent_outbound
                if (m.metadata or {}).get("failure_reason")
            },
            "windowOpen": _window_is_open(conversation),
            "aiProposal": _ai_proposal_json(account, conversation),
            "status_html": render_to_string(
                "conversations/_status_badges.html",
                {
                    "conversation": conversation,
                    "snapshot": get_conversation_state(conversation),
                    "now": timezone.now(),
                    "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
                    "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
                },
            ),
            "open": conversation.status == Conversation.Status.OPEN,
        }
    )


@login_required
@require_POST
def create_ticket_from_conversation(request, public_id: str):
    """Create a support ticket from an agent inbox conversation.

    POST params:
      priority   — p1/p2/p3/p4 (optional, default p3)
      subject    — override the derived subject (optional)
      force      — "1" to create even if an open ticket already exists
    """
    from apps.support.services.conversation import (
        DuplicateTicketError,
    )
    from apps.support.services.conversation import (
        create_ticket_from_conversation as _create,
    )

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    conversation = get_object_or_404(Conversation, public_id=public_id, account=account)

    _VALID_PRIORITIES = frozenset(["p1", "p2", "p3", "p4"])
    priority = request.POST.get("priority", "p3")
    if priority not in _VALID_PRIORITIES:
        priority = "p3"
    subject = request.POST.get("subject", "").strip() or None
    force = request.POST.get("force") == "1"

    try:
        from apps.support.models import SupportTicket as _SupportTicket

        ticket: _SupportTicket = _create(  # type: ignore[assignment]
            conversation,
            submitted_by=request.user,
            subject=subject,
            priority=priority,
            force=force,
        )
        messages.success(
            request,
            f"Support ticket {ticket.ticket_number} created.",
        )
        return redirect("support:detail", ticket_number=ticket.ticket_number)
    except DuplicateTicketError as exc:
        messages.warning(
            request,
            f"This conversation already has an open ticket ({exc.ticket.ticket_number}). "
            "Resolve or close it before creating a new one.",
        )
        return redirect("conversations:detail", public_id=public_id)
    except Exception:
        logger.exception("create_ticket_from_conversation failed for %s", public_id)
        messages.error(request, "Could not create ticket. Please try again.")
        return redirect("conversations:detail", public_id=public_id)


# ---------------------------------------------------------------------------
# Recommendation context — Steps 2 & 3 of the Instagram intelligence UI
# ---------------------------------------------------------------------------


def _active_recommendation_for(conversation: Conversation):
    """Return the most relevant RecommendationLog for the conversation sidebar.

    Selection order (deterministic, no proximity-based inference):
    1. A RecommendationLog already linked to this conversation (accepted/in-progress).
    2. The latest presented recommendation for the contact with no conversation yet.
    3. None — show nothing rather than guess.
    """
    try:
        from apps.insights.models import RecommendationLog

        # 1. Already executed against this conversation
        linked = (
            RecommendationLog.objects.filter(conversation=conversation)
            .select_related("insight")
            .order_by("-acted_at")
            .first()
        )
        if linked:
            return linked

        # 2. Latest presented (not yet acted on) whose Insight evidence references
        #    this contact's prior conversations. Since Insight has no direct contact FK
        #    yet, we scope via RecommendationLog rows whose policy_execution ran in the
        #    context of one of this contact's conversations (policy_execution →
        #    conversation → contact).  Fall back to account-scope only if that join
        #    yields nothing, so a contact with zero history still gets nothing rather
        #    than a stranger's recommendation.
        if conversation.contact_id:
            contact_rec = (
                RecommendationLog.objects.filter(
                    account=conversation.account,
                    status=RecommendationLog.Status.PRESENTED,
                    conversation__isnull=True,
                    insight__isnull=False,
                    policy_execution__conversation__contact=conversation.contact,
                )
                .select_related("insight")
                .order_by("-recommended_at")
                .first()
            )
            return contact_rec  # None if no contact-scoped rec exists — show nothing
    except Exception:
        logger.debug("_active_recommendation_for: insights app unavailable")
    return None


@login_required
@require_POST
def recommendation_act(request, pk: int):
    """Execute a recommendation from the conversation sidebar.

    POST /inbox/recommendations/<pk>/act/
    Payload: conversation_id (Conversation.pk)

    Returns the updated _recommendation_context.html partial for HTMX swap.
    """
    from apps.insights.actions import execute_recommendation, record_outcome_signal
    from apps.insights.models import RecommendationLog

    account = get_current_account(request)
    rec_log = get_object_or_404(RecommendationLog, pk=pk, account=account)

    raw_cid = (request.POST.get("conversation_id") or "").strip()
    try:
        conversation_id = int(raw_cid)
    except (ValueError, TypeError):
        return HttpResponse(status=400)
    conversation = get_object_or_404(Conversation, pk=conversation_id, account=account)

    try:
        execute_recommendation(
            rec_log,
            conversation=conversation,
            action_type=rec_log.action_type or conversation.channel,
        )
        # Only record the signal after confirmed execution; both calls share
        # the same try/except so a failure in either is logged and returns 500.
        record_outcome_signal(rec_log, "dm_sent_at")
    except Exception:
        logger.exception("recommendation_act failed for rec_log=%s", pk)
        return HttpResponse(status=500)

    html = render_to_string(
        "conversations/_recommendation_context.html",
        {"rec": rec_log, "conversation": conversation},
        request=request,
    )
    return HttpResponse(html)
