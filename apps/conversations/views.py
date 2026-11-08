"""Inbox: the operational-center UI, built as a projection over the Phase 1
spine (Conversation/Message/Event/Action Registry) — never a parallel state
store. See docs/plans — "UI/UX Principle: Complex Architecture, Simple Product".
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import (
    Count,
    OuterRef,
    Q,
    Subquery,
)
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render, resolve_url
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.models import Team
from apps.accounts.utils import get_current_account, viewing_as
from apps.conversations.actions import ActionError, inbox_send_started, run_action
from apps.conversations.api import conversation_window_is_open
from apps.conversations.flow_json import FIELD_TYPES as _FLOW_FIELD_TYPES
from apps.conversations.inbox_keys import inbox_key
from apps.conversations.media import media_json
from apps.conversations.models import (
    Conversation,
    ConversationForm,
    Event,
    FollowUp,
    FormResponse,
    Message,
    RoutingRule,
    SavedReply,
)
from apps.conversations.services import delete_conversation
from apps.conversations.state import (
    OVERDUE_WAITING,
    ConversationState,
    missed,
    needs_attention,
    snapshot_of,
    state_of,
    unanswered_q,
    waiting_counts,
    with_activity,
)
from apps.core.channels import hide_disabled_channels, visible_choices
from apps.core.htmx import is_background
from apps.core.module_gate import module_required

if TYPE_CHECKING:
    from django.http.response import HttpResponseBase

logger = logging.getLogger(__name__)

_PAGE_SIZE = 30


def _last_inbound(conversation) -> dict | None:
    """The customer's latest message: when, and its metadata (who took it)."""
    row = (
        Message.objects.filter(
            conversation=conversation, direction=Message.Direction.INBOUND
        )
        .order_by("-timestamp", "-id")
        .values("timestamp", "metadata")
        .first()
    )
    return dict(row) if row else None


def _last_handled_by(last_inbound: dict | None) -> str:
    """Who took the customer's latest message, in words (see services.HANDLED_LABELS)."""
    from apps.conversations.services import HANDLED_LABELS

    meta = (last_inbound or {}).get("metadata") or {}
    if "handled_by" not in meta:
        return ""
    return HANDLED_LABELS.get(meta["handled_by"], "")


def followup_card(f, viewer, *, now, zone, reply=False) -> dict:
    from apps.conversations import followups

    return followups.card(f, viewer, now=now, zone=zone, reply=reply)


def _as_dict(value) -> dict:
    """A JSON field read through a subquery: a dict on PostgreSQL, a string on SQLite."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def get_visible_conversation(account, public_id):
    """A conversation the inbox may show: 404 for one on a channel the admin switched off."""
    return get_object_or_404(
        # The pane and panel show the customer and the assignee, and read the account.
        hide_disabled_channels(
            Conversation.objects.select_related("contact", "account", "assigned_to")
        ),
        account=account,
        public_id=public_id,
    )


def _inbox_context(request, account) -> dict:
    now = timezone.now()
    view = (request.GET.get("view") or "all").strip()
    # The row's "Follow-up {day}" tag. A missed-chat follow-up is left out: the row's own
    # Missed badge already says it, and two labels for one fact read as two problems.
    _open_followups = (
        FollowUp.objects.filter(conversation=OuterRef("pk"), done_at__isnull=True)
        .exclude(source=FollowUp.Source.MISSED)
        .order_by("due_at")
    )
    if view == "needs_reply":  # legacy name for the same tab
        view = "needs_attention"

    # Every list is derived from Message rows via conversations.state - never MessageLog.
    # Views go by the conversation's stored status: every view but Closed shows open
    # conversations only, so the counts add up (All = the open ones, All + Closed = every one).
    open_ = Conversation.objects.filter(
        account=account, status=Conversation.Status.OPEN
    )
    # All, Assigned to me and Closed are newest first: by the stored last_message_at, so a
    # page is found without first working out every conversation's message facts (on 200,000
    # messages that alone took seconds). The page's own rows get those facts below. The waiting
    # views are defined by those facts, so they work them out for every conversation. A
    # conversation made without recording a message (no last_message_at) sorts by when it began.
    newest_first = (Coalesce("last_message_at", "created_at").desc(), "-id")
    stored_order = view in ("all", "assigned", "closed")
    if view == "missed":
        qs = missed(account, now)
    elif view == "assigned":
        qs = open_.filter(assigned_to=request.user).order_by(*newest_first)
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
        qs = Conversation.objects.filter(
            account=account, status=Conversation.Status.CLOSED
        ).order_by(*newest_first)
    elif view == "needs_attention":
        qs = needs_attention(account, now)
    else:
        view = "all"
        stored_order = True
        qs = open_.order_by(*newest_first)
    # Conversations on a channel the admin switched off are hidden from every list. The
    # assignee comes with the row: the row shows their name (a query per row before).
    qs = hide_disabled_channels(qs.select_related("contact", "assigned_to"))

    channel = (request.GET.get("channel") or "").strip()
    if channel:
        qs = qs.filter(channel=channel)

    # Combinable with any tab ("My + Overdue", "Unassigned + Overdue"): the
    # same waiting-on-us definition every tab already uses, just past the
    # stricter OVERDUE_WAITING cutoff instead of the 24h one.
    overdue_only = bool(request.GET.get("overdue"))
    if overdue_only:
        if stored_order:  # overdue is a message fact: this view now needs them all
            qs, stored_order = with_activity(qs), False
        qs = qs.filter(unanswered_q(), last_in__lte=now - OVERDUE_WAITING)

    # Search, within the view and channel: the customer's name, phone or email, or anything
    # said in the conversation.
    q = (request.GET.get("q") or "").strip()[:100]
    if q:
        from django.db.models import Exists

        qs = qs.filter(
            Q(contact__first_name__icontains=q)
            | Q(contact__last_name__icontains=q)
            | Q(contact__phone__icontains=q)
            | Q(contact__email__icontains=q)
            | Exists(
                Message.objects.filter(conversation=OuterRef("pk"), body__icontains=q)
            )
        )

    from apps.conversations import followup_copy, followups

    zone = followups.business_zone(account)

    # The row preview is the latest message, annotated here rather than fetched per row:
    # the list is re-rendered by the 8-second poll for every open inbox. Only the page's own
    # rows get it: the pager counts the plain list, so the count doesn't run these subqueries
    # for every conversation the account has.
    latest = Message.objects.filter(conversation=OuterRef("pk")).order_by(
        "-timestamp", "-id"
    )
    page = Paginator(qs, _PAGE_SIZE).get_page(request.GET.get("page"))
    if page.paginator.count:
        rows = qs
        if stored_order:
            # The page by stored order, then its rows with their message facts (one query: the
            # page's ids are a subquery).
            ids = qs[page.start_index() - 1 : page.end_index()].values("pk")
            rows = with_activity(
                Conversation.objects.filter(pk__in=ids).select_related(
                    "contact", "assigned_to"
                )
            ).order_by(*newest_first)
        page.object_list = list(
            rows.annotate(
                preview_body=Subquery(latest.values("body")[:1]),
                preview_direction=Subquery(latest.values("direction")[:1]),
                preview_status=Subquery(latest.values("status")[:1]),
                preview_metadata=Subquery(latest.values("metadata")[:1]),
                next_followup=Subquery(_open_followups.values("due_at")[:1]),
            )[
                slice(None)
                if stored_order
                else slice(page.start_index() - 1, page.end_index())
            ]
        )
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
        c.has_messages = c.preview_direction is not None
        # The latest message is ours and didn't go: the row says so, with why on hover,
        # instead of reading like any other "You: …".
        c.not_sent = c.preview_direction == Message.Direction.OUTBOUND and (
            c.preview_status in ("failed", "held")
        )
        c.not_sent_reason = (
            _failure(_as_dict(c.preview_metadata))[0] if c.not_sent else ""
        )
        # Closed is the stored status. An open conversation the customer has gone quiet on
        # (24h, and we spoke last) is "Waiting on them": it isn't closed, and stays in the open views.
        c.is_closed = c.status == Conversation.Status.CLOSED
        c.is_quiet = (
            not c.is_closed
            and c.snapshot.closed_reason == "inactive_24h"
            and not c.snapshot.missed
        )
        c.followup_tag = (
            followup_copy.inbox_tag(c.next_followup, now=now, zone=zone)
            if c.next_followup
            else ""
        )

    # Teammates in these conversations right now (one cache read for the page).
    from apps.conversations import presence

    rows = list(page)
    if rows:
        others = presence.for_conversations(
            [c.pk for c in rows],
            list(_team_members(account)),
            exclude_user_id=request.user.pk,
        )
        for c in rows:
            c.presence = others.get(c.pk)

    # The tabs' plain counts in one query.
    # Also one count per channel, so the channel filter only offers channels this business
    # has conversations on (SMS and Website Chat are choices whether used or not).
    choices = visible_choices(Conversation.Channel.choices)
    is_open = Q(status=Conversation.Status.OPEN)
    counts = hide_disabled_channels(
        Conversation.objects.filter(account=account)
    ).aggregate(
        assigned_count=Count("pk", filter=is_open & Q(assigned_to=request.user)),
        unassigned_count=Count("pk", filter=is_open & Q(assigned_to__isnull=True)),
        closed_count=Count("pk", filter=Q(status=Conversation.Status.CLOSED)),
        all_count=Count("pk", filter=is_open),
        **{
            f"channel__{value}": Count("pk", filter=Q(channel=value))
            for value, _ in choices
        },
    )
    channel_choices = [
        (value, label)
        for value, label in choices
        if counts.pop(f"channel__{value}") or value == channel
    ]
    return {
        "account": account,
        "now": now,
        "page": page,
        "view": view,
        "channel": channel,
        "channel_choices": channel_choices,
        "overdue_only": overdue_only,
        "q": q,
        # The two waiting counts need every open conversation's message facts: worked out
        # once for both (state.waiting_counts), by the same rules as their views.
        **waiting_counts(hide_disabled_channels(open_), now),
        **counts,
        "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
        "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
    }


@login_required
def inbox(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if request.GET.get("view") == "followups":
        # The old Reminders tab: follow-ups have one home now.
        return redirect("conversations:followups_due")
    return render(request, "conversations/inbox.html", _inbox_context(request, account))


@login_required
def inbox_feed(request):
    """Rendered inbox list (with its view choices), polled so new conversations appear live.

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

    context = _inbox_context(request, account)
    html = render_to_string("conversations/_inbox_body.html", context, request=request)

    if is_background(request):
        # The view and channel choices sit in the bar above the list (inbox.html) and carry
        # the counts, so they come along out of band; inside the digest, so a count that
        # changes on its own is still a change.
        html += render_to_string(
            "conversations/_inbox_views.html", {**context, "oob": True}, request=request
        )
        # The empty pane's "What needs you" counts (Phase 5D), the same way.
        html += render_to_string(
            "conversations/_inbox_today.html", {**context, "oob": True}, request=request
        )
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
    unaffected. On Messenger a reply typed here may go for 7 days (HUMAN_AGENT tag).
    """
    if conversation.channel == Conversation.Channel.FACEBOOK:
        fb = conversation.facebook_conversation
        return bool(fb and (fb.window_is_open or fb.human_agent_window_is_open))
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


def _ai_config(account, conversation, user=None) -> dict:
    """What the composer needs to show AI proposals. ``enabled`` is False whenever AI is off."""
    from apps.accounts import api as accounts_api
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
        "learnUrl": reverse("conversations:ai_learn", args=[conversation.public_id]),
        # Saving an answer changes what AI tells every customer, so only owners and admins.
        "canLearn": user is not None and accounts_api.is_account_admin(user, account),
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
    conversation = get_visible_conversation(account, public_id)
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
    get_visible_conversation(account, public_id)
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
    conversation = get_visible_conversation(account, public_id)
    ok, message = ai_api.apply_extra(
        account,
        conversation,
        request.POST.get("proposal"),
        request.POST.get("index"),
        request.user,
    )
    if not ok:
        return JsonResponse({"ok": False, "error": message}, status=400)
    # The composer's own line for the customer's leading follow-up, so a follow-up the AI
    # suggested shows there at once, in the same words as everywhere else.
    from apps.conversations import followup_copy, followups

    leading = followups.for_chat(conversation)
    return JsonResponse(
        {
            "ok": True,
            "message": message,
            "followup_label": followup_copy.composer_label(
                leading[0],
                now=timezone.now(),
                zone=followups.business_zone(account),
            )
            if leading
            else "",
            "proposal": ai_api.serialize(
                ai_api.current_proposal(account, conversation), conversation
            ),
        }
    )


@login_required
@require_POST
def ai_learn(request, public_id: str):
    """Save the team's reply to a customer question as a knowledge-base answer for AI."""
    from apps.accounts import api as accounts_api
    from apps.ai import api as ai_api

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"ok": False, "error": "no account"}, status=403)
    conversation = get_visible_conversation(account, public_id)
    if not accounts_api.is_account_admin(request.user, account):
        return JsonResponse(
            {"ok": False, "error": "Only an owner or admin can teach AI new answers."},
            status=403,
        )
    entry, error = ai_api.save_answer(
        account,
        request.POST.get("proposal"),
        request.POST.get("question", ""),
        request.POST.get("answer", ""),
    )
    if entry is None:
        return JsonResponse({"ok": False, "error": error}, status=400)
    return JsonResponse(
        {
            "ok": True,
            "message": "Saved. AI can answer this from now on.",
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
    conversation = get_visible_conversation(account, public_id)
    # Loaded into the two-pane inbox (static/js/inbox_pane.js) rather than as its own page.
    # Its forms then post with hx-post into the pane, so an action keeps the list beside it.
    in_pane = _in_pane(request)
    # A pane or details-panel action answers in place, so its confirmation rides along as a
    # toast; a plain form post shows it on the page it redirects to.
    # A quick action on a list row (close, assign to me, later): answered with its toast only.
    row_action = request.method == "POST" and _row_action(request)
    answers_in_place = in_pane or _panel_target(request) or row_action
    mutated = False

    if request.method == "POST":
        action = request.POST.get("action")
        ctx = {"account": account}
        sent: dict = {}
        try:
            if action == "reply":
                sent = run_action(
                    "reply",
                    ctx,
                    conversation=conversation,
                    body=request.POST.get("body", "").strip(),
                    # Sent again with the same key (a retry of an unconfirmed send), it
                    # returns the first send instead of messaging the customer twice.
                    idempotency_key=inbox_key(
                        conversation, request.user, request.POST.get("client_key")
                    ),
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
                if answers_in_place:
                    messages.success(request, f"Sent “{template.name}”.")
            elif action == "create_followup":
                from apps.conversations.followups import resolve_due_at

                choice = request.POST.get("when", "").strip()
                try:
                    due_at = resolve_due_at(
                        choice, request.POST.get("custom_due_at", ""), account=account
                    )
                except ValueError as exc:
                    raise ActionError(str(exc)) from exc
                from apps.conversations import followup_copy
                from apps.conversations.followups import local_label

                result = run_action(
                    "create_followup",
                    ctx,
                    conversation=conversation,
                    due_at=due_at,
                    note=request.POST.get("note", "").strip(),
                    created_by=request.user,
                )
                created = FollowUp.objects.get(pk=result["followup_id"])
                when = local_label(created.due_at, account)
                (
                    messages.warning
                    if result.get("note") == "too_long"
                    else messages.success
                )(
                    request,
                    followup_copy.set_for(when)
                    if result["created"]
                    else followup_copy.kept_existing(when, result.get("note", "")),
                )
            elif action == "assign":
                assignee = _resolve_assignee(account, request)
                run_action(
                    "assign_conversation",
                    ctx,
                    conversation=conversation,
                    user=assignee,
                    assigned_by=request.user,
                )
                if answers_in_place:
                    messages.success(
                        request,
                        "Unassigned."
                        if assignee is None
                        else "Assigned to you."
                        if assignee.pk == request.user.pk
                        else f"Assigned to {assignee.get_full_name() or assignee.get_username()}.",
                    )
            elif action == "add_note":
                run_action(
                    "add_internal_note",
                    ctx,
                    conversation=conversation,
                    body=request.POST.get("body", "").strip(),
                    author=request.user,
                )
                if answers_in_place:
                    messages.success(request, "Note added.")
            elif action == "mark_read":
                conversation.mark_read()
            elif action == "close":
                resolution = request.POST.get("resolution", "")
                if resolution and resolution not in Conversation.Resolution.values:
                    raise ActionError("That's not a valid close reason.")
                before = _close_state(conversation)
                conversation.close(
                    resolution=resolution, actor=f"user:{request.user.pk}"
                )
                if answers_in_place:
                    from apps.accounts.utils import flash

                    flash(
                        request,
                        "success",
                        "Closed. It reopens if the customer writes again.",
                        undo=_undo_close_url(conversation, before, request.user),
                    )
            elif action == "reopen":
                conversation.reopen(actor=f"user:{request.user.pk}")
                if answers_in_place:
                    messages.success(request, "Conversation reopened.")
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
                if answers_in_place:
                    messages.success(request, f"Started “{form.name}”.")
            elif action == "pause_automation":
                choice = request.POST.get("hours", "")
                hours = None if choice == "forever" else float(choice or 4)
                conversation.pause_automation(hours)
                messages.success(
                    request,
                    "Automations are paused in this conversation"
                    + (
                        " until you turn them back on."
                        if hours is None
                        else f" for {hours:g} hour{'' if hours == 1 else 's'}."
                    ),
                )
            elif action == "resume_automation":
                conversation.resume_automation()
                messages.success(request, "Automations can answer here again.")
            elif action == "stop_run":
                from apps.automation import api as automation_api
                from apps.automation.models import WorkflowRun

                run = WorkflowRun.objects.filter(
                    workflow__account=account,
                    contact=conversation.contact,
                    public_id=request.POST.get("run", ""),
                ).first()
                if run is None:
                    raise ActionError("That automation run wasn't found.")
                try:
                    automation_api.cancel_run(run)
                except automation_api.RunActionError as exc:
                    raise ActionError(str(exc)) from exc
                messages.success(
                    request, f"Stopped {run.workflow.name} for this customer."
                )
            elif action == "run_automation":
                from apps.automation import api as automation_api

                workflow = next(
                    (
                        w
                        for w in automation_api.manual_workflows(account)
                        if w.slug == request.POST.get("workflow", "")
                    ),
                    None,
                )
                if workflow is None:
                    raise ActionError("Choose an automation to run.")
                counts = automation_api.enroll_manually(
                    workflow,
                    [conversation.contact],
                    context={"conversation_id": conversation.public_id},
                )
                if counts["started"]:
                    messages.success(request, f"Started {workflow.name}.")
                elif counts["already"]:
                    messages.info(
                        request, f"{workflow.name} is already running for them."
                    )
                else:
                    raise ActionError(
                        f"{workflow.name} couldn't start for this customer."
                    )
            elif action == "cancel_form":
                from apps.conversations import forms as conversation_forms

                response = conversation_forms.active_response_for(conversation)
                if response is not None:
                    conversation_forms.cancel(response)
                    messages.success(
                        request, f"Stopped waiting for “{response.form.name}”."
                    )
        except ActionError as exc:
            if _is_ajax(request):
                return JsonResponse({"ok": False, "error": str(exc)}, status=400)
            messages.error(request, str(exc))
        else:
            if _is_ajax(request):
                # A repeated send whose first attempt Meta may or may not have taken: the
                # inbox keeps saying "Not confirmed" rather than "Sent".
                if (sent or {}).get("unconfirmed"):
                    return JsonResponse({"ok": True, "unconfirmed": True})
                return JsonResponse({"ok": True})
            mutated = True
        if row_action:
            # Nothing to swap: the toast (Close keeps its Undo) and the list re-read.
            answer = HttpResponse(status=204)
            _messages_to_toast(request, answer)
            answer["HX-Trigger"] = "inbox-refresh"
            return answer
        if _panel_target(request):
            return _panel_response(
                request,
                account,
                _panel_conversation(account, public_id),
                after_action=True,
            )
        if not in_pane:
            return redirect("conversations:detail", public_id=public_id)
        # In the pane: re-render it below, from fresh state, instead of redirecting away.
        conversation = get_visible_conversation(account, public_id)

    if _panel_target(request):
        # A panel action's answer (directly, or through the redirect a tag or follow-up view
        # sends back to here): the panel again, not the whole pane.
        return _panel_response(request, account, conversation, after_action=True)

    was_unread = conversation.is_unread
    # Support looking "as" the business mustn't clear its unread, and neither does a peek:
    # stepping through the list with J/K (static/js/inbox_pane.js) marks a conversation read
    # only once it has stayed open a moment, with action=mark_read.
    if viewing_as(request) is None and request.GET.get("peek") != "1":
        conversation.mark_read()
    thread, has_earlier = _recent_thread(conversation, unread=was_unread)
    snapshot = state_of(conversation)
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
        "typingUrl": reverse("conversations:typing", args=[conversation.public_id]),
        "lastId": thread[-1].id if thread else 0,
        # Older messages than the thread shows ("Show earlier messages", ?before=).
        "hasEarlier": has_earlier,
        # Where "New messages" goes (Phase 5E): the first of what the customer wrote since the
        # business last spoke, the same run _recent_thread keeps in the window.
        **_first_unread(thread, unread=was_unread),
        # The browser's clock can be off; a first estimate of how far, refined by every
        # feed poll (which measures the round trip).
        "serverNow": now.isoformat(),
        # A poll redirected here means the session ended (chat.js says "signed out").
        "loginUrl": resolve_url(settings.LOGIN_URL),
        "open": conversation.status == Conversation.Status.OPEN,
        # Only WhatsApp enforces a messaging window today; other channels report
        # it open so the composer behaves as it always has for them.
        "windowOpen": _window_is_open(conversation),
        "ai": _ai_config(account, conversation, request.user),
        "messages": [
            {**_message_json(m, conversation), "automate": offers.get(m.id)}
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
    approved_templates = _approved_templates(account, conversation)
    from apps.conversations import followup_copy

    # The details panel (_conversation_panel.html) is not built here: it loads on its own
    # (conversations:panel) once the conversation shows, so opening one costs the thread and
    # the composer only. The composer's Next steps still says whether a follow-up is set and
    # whether the customer is tracked, so those two come with the pane.
    pane_context = {
        "account": account,
        "now": now,
        "conversation": conversation,
        "chat_config": chat_config,
        "snapshot": snapshot,
        "approved_templates": approved_templates,
        **_followup_context(request, account, conversation, now=now),
        **_crm_context(account, conversation),
        "followup_copy": followup_copy,
        "team_members": _team_members(account),
        "in_pane": in_pane,
        "detail_url": reverse("conversations:detail", args=[conversation.public_id]),
        "panel_url": reverse("conversations:panel", args=[conversation.public_id]),
    }
    # The inbox list (templates/conversations/inbox.html, ≥lg) loads a conversation into its
    # own pane via a plain background GET — see static/js/inbox_pane.js — rather than a full
    # navigation, so opening one never re-loads the shell or the list beside it. Below lg there
    # is no pane to load into, so a row's own href still points here and gets the full page below.
    if in_pane:
        pane = render(request, "conversations/_conversation_pane.html", pane_context)
        # The same address is the full page without these headers.
        pane["Vary"] = "HX-Request, HX-Target"
        pane["Cache-Control"] = "no-store"
        # Fetched ahead (a hovered row, the next one after J) and maybe never shown: a
        # queued confirmation is left for the load that does show.
        if request.headers.get("X-Inbox-Prefetch") != "1":
            _messages_to_toast(request, pane)
        if mutated:
            # Assignment, close and reopen change the list beside the pane: refresh it now.
            pane["HX-Trigger"] = "inbox-refresh"
        return pane

    # A full page load of a conversation's own address (a reload after opening it in the
    # pane, a bookmark, a link, a new tab) shows it the way it was opened: inside the inbox,
    # in the pane beside the list. Below 768px the pane shows alone (.has-conversation), with
    # Back to the list, as the standalone page did.
    return render(
        request,
        "conversations/inbox.html",
        {
            **_inbox_context(request, account),
            **pane_context,
            # Its forms post into the pane, as they do once it has been opened there.
            "in_pane": True,
            "open_in_pane": True,
        },
    )


def _in_pane(request) -> bool:
    return (
        is_background(request)
        and request.headers.get("HX-Target") == "conversation-pane-inner"
    )


def _row_action(request) -> bool:
    """A quick action from a list row (_inbox_body.html), which only needs its toast back."""
    return is_background(request) and request.headers.get("X-Inbox-Row") == "1"


def _panel_target(request) -> bool:
    """A request from inside the details panel, whose answer replaces the panel only."""
    return is_background(request) and request.headers.get("HX-Target") == "cv-panel"


# How much of a thread a conversation opens with: the latest THREAD_WINDOW messages, or from
# the first unread one when that is further back, up to THREAD_MAX. Older ones load on demand
# (messages_feed ?before=), so a long chat costs no more to open than a short one.
THREAD_WINDOW = 50
THREAD_MAX = 200


def _recent_thread(conversation, *, unread: bool) -> tuple[list, bool]:
    """The messages a conversation opens with, oldest first, and whether older ones exist.

    One query for up to THREAD_MAX rows. Unread here means what the customer wrote since the
    business last spoke (``is_unread`` is a flag on the conversation, not a per-message mark),
    so the window reaches back to the first of those messages when there are more than fit."""
    latest = list(
        conversation.messages.select_related(
            "whatsapp_message", "instagram_message", "facebook_message"
        ).order_by("-timestamp", "-id")[:THREAD_MAX]
    )
    keep = THREAD_WINDOW
    if unread:
        for i, m in enumerate(latest):
            if m.direction != Message.Direction.INBOUND:
                break
            keep = max(keep, i + 1)
    shown = latest[:keep]
    has_earlier = len(latest) > keep or len(latest) == THREAD_MAX
    return shown[::-1], has_earlier


def _first_unread(thread: list, *, unread: bool) -> dict:
    """``firstUnreadId`` and ``unreadCount`` for the thread's "New messages" divider: the
    customer's messages after the business last spoke, when the conversation is unread."""
    if not unread:
        return {"firstUnreadId": None, "unreadCount": 0}
    run = []
    for m in reversed(thread):
        if m.direction != Message.Direction.INBOUND:
            break
        run.append(m)
    if not run:
        return {"firstUnreadId": None, "unreadCount": 0}
    return {"firstUnreadId": run[-1].id, "unreadCount": len(run)}


def _failure(meta: dict | None) -> tuple[str | None, dict | None]:
    """Why a message didn't go, in plain words, and where to fix it when the cause is the
    WhatsApp account itself (billing, display name, connection) rather than this message.

    Worked out from the stored error code each time it's shown, not from the sentence stored
    when it failed: wording added since (or a code stored in another shape) reaches old
    messages too, so a raw "(error 131042)" doesn't linger."""
    from apps.whatsapp.friendly_errors import failure

    meta = meta or {}
    return failure(meta.get("error_code"), meta.get("failure_reason"))


def _message_json(m, conversation) -> dict:
    """A message as the thread (static/js/chat.js) shows it."""
    meta = m.metadata or {}
    reason, fix = _failure(meta)
    from apps.ai.proposals import plain_spaces

    return {
        "id": m.id,
        "direction": m.direction,
        # The AI's narrow no-break spaces made words run together; proposals.plain_spaces now
        # stops them at the source, and what it sent before shows with plain spaces.
        "body": plain_spaces(m.body) if meta.get("sent_by") == "ai" else m.body,
        "ts": m.timestamp.isoformat(),
        "status": m.status,
        "failureReason": reason,
        "failureFix": fix,
        "byAi": meta.get("sent_by") == "ai",
        # The browser's key for a reply sent from the inbox (inbox_keys), so its
        # "Sending…" or "Not confirmed" bubble is matched to exactly this message.
        "clientKey": meta.get("client_key") or None,
        "media": media_json(m, conversation.public_id),
    }


def _followup_context(
    request, account, conversation, *, now, panel=False, last_inbound=None
) -> dict:
    """The customer's follow-ups, this chat's own first; the composer and the side panel
    describe the first exactly as the Follow-ups page does."""
    from apps.conversations import followup_copy
    from apps.conversations import followups as followup_service

    customer_followups = followup_service.for_chat(conversation, now=now)
    open_followup = customer_followups[0] if customer_followups else None
    followup = None
    if open_followup:
        # The business's timezone only matters for describing one (a query when read).
        zone = followup_service.business_zone(account)
        followup = followup_card(open_followup, request.user, now=now, zone=zone)
        followup["composer"] = followup_copy.composer_label(
            open_followup, now=now, zone=zone
        )
        others = len(customer_followups) - 1
        name = conversation.contact.display_name
        followup["others"] = others
        followup["all_label"] = (
            f"{followup_copy.others_label(others)} · " if others else ""
        ) + followup_copy.all_for(name)
        followup["wrote_since"] = followup_copy.wrote_since(name)
    out = {"open_followup": open_followup, "followup": followup}
    if panel:
        # followups.open_after_customer_replied for the one follow-up shown, from the
        # customer's latest message the panel has already read: set by a person on this
        # chat, still open, and the customer has written since.
        last_at = (last_inbound or {}).get("timestamp")
        out["followup_still_needed"] = bool(
            open_followup is not None
            and last_at is not None
            and open_followup.conversation_id == conversation.pk
            and open_followup.source not in followup_service.WAITING_SOURCES
            and open_followup.created_at < last_at
        )
        out["later_choices"] = followup_service.later_choices(account)
    return out


def _usable(account) -> frozenset:
    """The features this business can use now (memoised per request by billing)."""
    from apps.billing import api as billing_api

    return billing_api.nav_state(account)["usable"]


def _crm_context(account, conversation) -> dict:
    """Whether the customer is tracked: an open lead, else an open deal. Only asked when Sales
    is usable, the only time the pane and panel show it."""
    open_lead = open_deal = None
    if "sales" not in _usable(account):
        return {"open_lead": None, "open_deal": None}
    try:
        from apps.crm.models import Deal, Lead

        open_lead = Lead.objects.filter(
            account=account,
            contact=conversation.contact,
            status__in=[Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED],
        ).first()
        # Giving a lead a value turns it straight into a deal, which leaves no open lead.
        # A customer in the pipeline is still being tracked, so the panel must say so.
        if open_lead is None:
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
    return {"open_lead": open_lead, "open_deal": open_deal}


def _panel_context(request, account, conversation, *, page=False) -> dict:
    """Everything the details panel shows beyond the conversation itself. ``page``: the panel
    as a page of its own (no scripts), which lists earlier conversations in place instead of
    loading them when Activity opens."""
    from apps.conversations import followup_copy

    if not hasattr(conversation, "last_any"):
        # Reached through a redirect back from a tag or follow-up view: the conversation
        # without the panel's facts.
        conversation = _panel_conversation(account, conversation.public_id)
    now = timezone.now()
    usable = _usable(account)
    open_order = None
    if "orders" in usable:  # the panel shows an open order only then
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

    try:
        from apps.automation.api import activity_for_contact, contact_automations

        automation_activity = activity_for_contact(account, conversation.contact)
        automation_now = contact_automations(account, conversation.contact)
    except Exception:
        # never let a side panel cost the owner their conversation
        automation_activity, automation_now = [], {"running": [], "manual": []}

    from apps.conversations import forms as conversation_forms

    active_form_response = conversation_forms.active_response_for(conversation)
    # Only forms that would actually launch here: a Flow that isn't published (or needs
    # republishing) can't be sent on WhatsApp, and offering it would just end in an error.
    published_forms = (
        [
            f
            for f in ConversationForm.objects.filter(
                account=account, status=ConversationForm.Status.PUBLISHED
            ).order_by("name")
            if conversation_forms.can_start(f, conversation)
        ]
        if active_form_response is None and "automations" in usable
        else []
    )
    last_inbound = _last_inbound(conversation)
    snapshot = snapshot_of(conversation, now)
    notes = list(conversation.notes.select_related("author"))
    paid_n = getattr(conversation, "paid_n", 0)
    meta = (last_inbound or {}).get("metadata") or {}

    return {
        "account": account,
        "now": now,
        "conversation": conversation,
        "automation_activity": automation_activity,
        "automation_now": automation_now,
        "last_handled_by": _last_handled_by(last_inbound),
        "active_recommendation": _active_recommendation_for(conversation),
        # Newest first: the panel shows the latest, with the rest one tap away.
        "notes": notes[::-1],
        **_crm_context(account, conversation),
        "open_order": open_order,
        "paid": {
            "n": paid_n,
            "total": conversation.paid_total
            if paid_n and conversation.paid_cur_lo == conversation.paid_cur_hi
            else None,
            "currency": conversation.paid_cur_lo if paid_n else "",
        },
        # Cheap facts a support agent wants without leaving the conversation:
        # how long this has been a customer, and whether this is a first
        # contact or one of a longer history. Computed here, not stored.
        "conversation_count": conversation.contact_conversations,
        "earlier_count": max(conversation.contact_conversations - 1, 0),
        "earlier_conversations": _earlier_conversations(account, conversation)
        if page
        else None,
        "status_row": _status_row(conversation, snapshot),
        "reply_window": _reply_window(
            conversation, snapshot.last_customer_message_at, now
        ),
        "automation_state": _automation_state(conversation, now),
        # "Why didn't it reply?" only when the customer's latest message was left for the
        # team: no automation, form or waiting run took it.
        "left_for_team": last_inbound is not None and meta.get("handled_by") == "",
        **_followup_context(
            request,
            account,
            conversation,
            now=now,
            panel=True,
            last_inbound=last_inbound,
        ),
        "followup_copy": followup_copy,
        "active_form_response": active_form_response,
        "published_forms": published_forms,
        "can_delete": _can_delete(request, account),
        "detail_url": reverse("conversations:detail", args=[conversation.public_id]),
    }


def _panel_response(request, account, conversation, *, after_action=False):
    """The details panel, for loading into the pane (#cv-panel) after the conversation shows.

    After an action taken in it, the answer also carries what else on screen that action can
    change, so nothing is left stale: the composer's Next steps (a follow-up shows there),
    swapped out of band, and ``inbox-refresh`` for the row in the list."""
    context = {**_panel_context(request, account, conversation), "in_pane": True}
    html = render_to_string(
        "conversations/_conversation_panel.html", context, request=request
    )
    if after_action:
        context["oob"] = True
        context["approved_templates"] = _approved_templates(account, conversation)
        html += render_to_string(
            "conversations/_composer_next.html", context, request=request
        )
    response = HttpResponse(html)
    response["Vary"] = "HX-Request, HX-Target"
    response["Cache-Control"] = "no-store"
    if after_action:
        _messages_to_toast(request, response)
        # The list row, and the pane's header (chat.js polls at once on cv-changed): a
        # Status row's Close, Reopen or Assign changes both.
        response["HX-Trigger"] = "inbox-refresh, cv-changed"
    return response


def _approved_templates(account, conversation) -> list:
    """The WhatsApp templates the composer can send here (none on other channels)."""
    if conversation.channel != Conversation.Channel.WHATSAPP:
        return []
    from apps.whatsapp.models import MessageTemplate

    return list(
        MessageTemplate.objects.filter(
            account=account,
            approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        ).order_by("name")
    )


def _panel_conversation(account, public_id):
    """The conversation as the details panel needs it, in one query: how many conversations
    its customer has had, the message facts its Status rows are worked out from
    (state.with_activity), and, where Orders is usable, what the customer has paid."""
    count = (
        Conversation.objects.filter(account=account, contact=OuterRef("contact"))
        .order_by()
        .values("contact")
        .annotate(n=Count("pk"))
        .values("n")
    )
    qs = with_activity(
        hide_disabled_channels(
            Conversation.objects.select_related("contact", "account", "assigned_to")
        ).annotate(contact_conversations=Coalesce(Subquery(count), 0))
    )
    if "orders" in _usable(account):
        qs = qs.annotate(**_paid_facts(account))
    return get_object_or_404(qs, account=account, public_id=public_id)


def _paid_facts(account) -> dict:
    """The customer's paid orders, as subqueries on their conversation: how many, the total,
    and the lowest and highest currency (a total is only shown when those are the same)."""
    from django.db.models import Max, Min, Sum

    from apps.commerce.models import Order

    paid = Order.objects.filter(
        account=account, contact=OuterRef("contact"), status=Order.Status.PAID
    ).order_by()

    def one(aggregate):
        return Subquery(paid.values("contact").annotate(v=aggregate).values("v")[:1])

    return {
        "paid_n": Coalesce(one(Count("pk")), 0),
        "paid_total": one(Sum("total")),
        "paid_cur_lo": one(Min("currency")),
        "paid_cur_hi": one(Max("currency")),
    }


def _span(delta) -> str:
    """A length of time as the panel's Status rows say it: "40 min", "6h", "4 days"."""
    minutes = max(round(delta.total_seconds() / 60), 1)
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{round(minutes / 60)}h"
    return f"{round(minutes / (24 * 60))} days"


def _reply_window(conversation, last_in, now) -> dict | None:
    """The Reply window row: open until when, or closed since when. The 24 hours run from the
    customer's latest message, as each channel's own record counts them; on Messenger a person
    may still reply for 7 days (HUMAN_AGENT). None on a channel without a window."""
    import datetime

    windows = {
        Conversation.Channel.WHATSAPP: [24],
        Conversation.Channel.INSTAGRAM: [24],
        Conversation.Channel.FACEBOOK: [24, 24 * 7],
    }
    hours = windows.get(conversation.channel)
    if hours is None:
        return None
    if last_in is None:
        return {"open": False, "label": "Waiting for their first message"}
    for h in hours:
        closes = last_in + datetime.timedelta(hours=h)
        if closes > now:
            return {"open": True, "label": f"Open · closes in {_span(closes - now)}"}
    closed = last_in + datetime.timedelta(hours=hours[-1])
    return {"open": False, "label": f"Closed {_span(now - closed)} ago"}


def _automation_state(conversation, now) -> dict:
    """The Automations row: on, paused until a time, or paused until turned back on."""
    until = conversation.automation_paused_until
    if not until or until <= now:
        return {"paused": False, "label": "On"}
    if (until - now).days > 365:  # pause_automation(None): ten years, i.e. no end
        return {"paused": True, "label": "Paused"}
    local = timezone.localtime(until)
    same_day = local.date() == timezone.localtime(now).date()
    return {
        "paused": True,
        "label": f"Paused until {local:%H:%M}"
        if same_day
        else f"Paused until {local.day} {local:%b, %H:%M}",
    }


def _status_row(conversation, snapshot) -> dict:
    """The Status row in the stored-status sense the list uses: Closed only when someone
    closed it; an open chat we spoke last in, quiet for a day, is Waiting on them."""
    if conversation.status == Conversation.Status.CLOSED:
        return {"label": "Closed", "open": False}
    if snapshot.missed:
        return {"label": "Open · missed", "open": True}
    if snapshot.state == ConversationState.WAITING_FOR_AGENT:
        return {"label": "Open · waiting for you", "open": True}
    if snapshot.closed_reason == "inactive_24h":
        return {"label": "Waiting on them", "open": True}
    return {"label": "Open", "open": True}


def _earlier_conversations(account, conversation) -> list:
    """The customer's other conversations, latest first, five at most."""
    return list(
        hide_disabled_channels(
            Conversation.objects.filter(account=account, contact=conversation.contact)
        )
        .exclude(pk=conversation.pk)
        .order_by("-last_message_at", "-created_at")[:5]
    )


# Closing answers with an Undo (E in the inbox, or Close as in the pane) that restores what was
# there before: open, the assignee and the close reason, recorded at close time and signed into
# the Undo's address, so nothing is kept on the server and nothing can be forged.
UNDO_CLOSE_SALT = "conversations.undo-close"
UNDO_CLOSE_SECONDS = 120


def _close_state(conversation) -> dict:
    return {
        "status": conversation.status,
        "assignee": conversation.assigned_to_id,
        "resolution": conversation.resolution or "",
        "closed_at": conversation.closed_at.isoformat()
        if conversation.closed_at
        else None,
    }


def _undo_close_url(conversation, before: dict, user) -> str:
    from django.core import signing

    token = signing.dumps(
        {
            "c": conversation.public_id,
            "u": user.pk,
            "before": before,
            # What the close left, so a later change (the customer wrote, a teammate
            # reopened or closed it again) isn't overwritten by an old Undo.
            "closed_at": conversation.closed_at.isoformat(),
        },
        salt=UNDO_CLOSE_SALT,
        compress=True,
    )
    return (
        reverse("conversations:undo_close", args=[conversation.public_id])
        + "?t="
        + token
    )


@login_required
@require_POST
def undo_close(request, public_id: str):
    """Put a conversation back as it was before it was closed (the close toast's Undo)."""
    import datetime

    from django.core import signing

    from apps.accounts.utils import hx_toast

    account = get_current_account(request)
    if account is None:
        raise Http404
    conversation = get_visible_conversation(account, public_id)
    try:
        data = signing.loads(
            request.GET.get("t", ""),
            salt=UNDO_CLOSE_SALT,
            max_age=UNDO_CLOSE_SECONDS,
        )
    except signing.BadSignature:
        data = None
    response = HttpResponse()
    if (
        not data
        or data.get("c") != conversation.public_id
        or data.get("u") != request.user.pk
    ):
        return hx_toast(response, "warning", "That Undo has expired.")
    unchanged = (
        conversation.status == Conversation.Status.CLOSED
        and conversation.closed_at is not None
        and conversation.closed_at.isoformat() == data.get("closed_at")
    )
    if not unchanged:
        return hx_toast(
            response, "info", "It changed since it was closed, so it was left as it is."
        )
    before = data["before"]
    conversation.status = before["status"]
    conversation.resolution = before["resolution"]
    conversation.closed_at = (
        datetime.datetime.fromisoformat(before["closed_at"])
        if before["closed_at"]
        else None
    )
    fields = ["status", "resolution", "closed_at", "updated_at"]
    if conversation.assigned_to_id != before["assignee"]:
        conversation.assigned_to_id = before["assignee"]
        fields.append("assigned_to")
    conversation.save(update_fields=fields)
    if conversation.status == Conversation.Status.OPEN:
        Event.objects.create(
            account_id=conversation.account_id,
            type="conversation.reopened",
            occurred_at=timezone.now(),
            source="conversations",
            actor=f"user:{request.user.pk}",
            subject_type="conversation",
            subject_id=str(conversation.pk),
            payload={"undo": True},
        )
    response["HX-Trigger"] = "inbox-refresh"
    return hx_toast(response, "success", "Put back as it was.")


@login_required
def conversation_panel(request, public_id: str):
    """The details panel on its own: loaded into the pane once the conversation shows, or
    as a page of its own where scripts don't run (the pane links here in a <noscript>)."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    conversation = _panel_conversation(account, public_id)
    if request.GET.get("part") == "history":
        # Activity's earlier conversations, loaded the first time it opens.
        response = render(
            request,
            "conversations/_panel_history.html",
            {"earlier_conversations": _earlier_conversations(account, conversation)},
        )
        response["Vary"] = "HX-Request, HX-Target"
        response["Cache-Control"] = "no-store"
        return response
    if is_background(request):
        return _panel_response(request, account, conversation)
    return render(
        request,
        "conversations/panel_page.html",
        {
            **_panel_context(request, account, conversation, page=True),
            "in_pane": False,
        },
    )


_TOAST_TYPES = {"error": "danger", "warning": "warning", "info": "info"}


def _messages_to_toast(request, response) -> None:
    """A pane swap renders no page shell, so queued flash messages would surface on some
    later page. Carry the latest one as an X-Toast instead (static/js/app.js shows it)."""
    from apps.accounts.utils import UNDO_TAG, hx_toast

    last = None
    for message in messages.get_messages(request):
        last = message
    if last is not None:
        tags = (last.extra_tags or "").split()
        undo = next((t[len(UNDO_TAG) :] for t in tags if t.startswith(UNDO_TAG)), None)
        hx_toast(
            response, _TOAST_TYPES.get(last.level_tag, "success"), str(last), undo=undo
        )


def _can_delete(request, account) -> bool:
    """Owners and admins only, and never while an operator is viewing as the business. The
    membership was already read while resolving the account (current_membership)."""
    from apps.accounts.models import Membership
    from apps.accounts.utils import current_membership

    membership = current_membership(request)
    return (
        viewing_as(request) is None
        and membership is not None
        and membership.account_id == account.pk
        and membership.role in (Membership.Role.OWNER, Membership.Role.ADMIN)
    )


@login_required
@require_POST
def conversation_delete(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    conversation = get_visible_conversation(account, public_id)
    if not _can_delete(request, account):
        messages.error(request, "Only owners and admins can delete conversations.")
        return redirect("conversations:detail", public_id=public_id)
    delete_conversation(conversation, actor=request.user)
    messages.success(request, "Conversation deleted.")
    return redirect("conversations:inbox")


@login_required
def followups_due(request):
    """The Follow-ups page: one row per customer, in Waiting / Due / Coming up.

    Built from ``followups.rows_for`` and worded by ``followup_copy``
    (docs/design/followups-interaction-contract.md). Mine or the whole team; ``?contact=``
    narrows it to one customer.
    """
    from apps.accounts.models import Membership
    from apps.contacts.models import Contact
    from apps.conversations import followup_copy as copy
    from apps.conversations import followups

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    now = timezone.now()
    scope = "team" if request.GET.get("list") == "team" else "mine"
    contact = None
    if request.GET.get("contact"):
        contact = get_object_or_404(
            Contact, account=account, public_id=request.GET["contact"]
        )
    zone = followups.business_zone(account)
    grouped = followups.rows_for(
        request.user, account, scope=scope, contact=contact, now=now
    )

    def card(f, *, reply=False):
        return followup_card(f, request.user, now=now, zone=zone, reply=reply)

    sections = []
    for key in followups.SECTION_ORDER:
        rows = [
            {
                "contact": row.contact,
                "main_label": copy.REPLY_NOW
                if row.main_action == "reply"
                else copy.OPEN_CHAT
                if row.primary.conversation_id
                else "Open customer",
                "main": card(row.primary, reply=row.main_action == "reply"),
                "others": [card(f) for f in row.others],
            }
            for row in grouped[key]
        ]
        sections.append(
            {
                "key": key,
                "title": copy.SECTION_TITLES[key],
                "empty": copy.EMPTY_STATES[key],
                "rows": rows,
            }
        )
    membership = Membership.objects.filter(account=account, user=request.user).first()
    teammates = [
        m.user
        for m in Membership.objects.filter(account=account, user__is_active=True)
        .select_related("user")
        .order_by("user__first_name", "user__email")
    ]
    return render(
        request,
        "conversations/followups_due.html",
        {
            "account": account,
            "copy": copy,
            "list": scope,
            "contact": contact,
            "sections": sections,
            "mine_count": followups.due_for(request.user, account, now=now),
            "team_count": followups.due_for_team(account, now=now),
            "teammates": teammates,
            "emails_on": bool(membership and membership.followup_emails),
            "zone_name": now.astimezone(zone).tzname(),
            "later_choices": followups.later_choices(account, now),
        },
    )


@login_required
@require_POST
def followup_create(request):
    """Set a follow-up for a customer from their contact or lead page.

    It belongs to the chat named in ``conversation`` (one of theirs, on a channel that's
    on), or to no chat for a customer who has none; same choices and messages as the chat.
    """
    from apps.contacts.models import Contact
    from apps.conversations import followup_copy, followups

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    contact = get_object_or_404(
        Contact, account=account, public_id=request.POST.get("contact", "")
    )
    conversation = None
    if request.POST.get("conversation"):
        conversation = get_object_or_404(
            hide_disabled_channels(Conversation.objects.all()),
            account=account,
            contact=contact,
            public_id=request.POST["conversation"],
        )
    note = request.POST.get("note", "").strip()
    try:
        try:
            due_at = followups.resolve_due_at(
                request.POST.get("when", "").strip(),
                request.POST.get("custom_due_at", ""),
                account=account,
            )
        except ValueError as exc:
            raise ActionError(str(exc)) from exc
        if conversation is not None:
            result = run_action(
                "create_followup",
                {"account": account},
                conversation=conversation,
                due_at=due_at,
                note=note,
                created_by=request.user,
            )
            followup = FollowUp.objects.get(pk=result["followup_id"])
            created, note_result = result["created"], result.get("note", "")
        else:
            followup, created = followups.create(
                account, contact, due_at=due_at, note=note, created_by=request.user
            )
            note_result = getattr(followup, "note_result", "")
        when = followups.local_label(followup.due_at, account)
        (messages.warning if note_result == "too_long" else messages.success)(
            request,
            followup_copy.set_for(when)
            if created
            else followup_copy.kept_existing(when, note_result),
        )
    except ActionError as exc:
        messages.error(request, str(exc))
    return _back(request, "conversations:followups_due")


@login_required
@require_POST
def followup_complete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    from apps.conversations import followup_copy

    followup = get_object_or_404(FollowUp, account=account, pk=pk)
    try:
        run_action(
            "complete_followup",
            {"account": account},
            followup=followup,
            by=request.user,
        )
        from apps.accounts.utils import flash
        from apps.core.undo import undo_url

        flash(
            request,
            "success",
            followup_copy.MARKED_DONE,
            undo=undo_url(request, "followup_done", followup.pk),
        )
    except ActionError as exc:
        messages.error(request, str(exc))
    return _back(request, "conversations:followups_due")


@login_required
@require_POST
def followup_action(request, pk, action):
    """Later, Give to…, Edit note, Not needed or reopen, on one follow-up."""
    from apps.conversations import followup_copy, followups

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    followup = get_object_or_404(FollowUp, account=account, pk=pk)
    ctx = {"account": account}
    try:
        if action == "snooze":
            run_action(
                "snooze_followup",
                ctx,
                followup=followup,
                when=request.POST.get("when", ""),
                custom=request.POST.get("custom_due_at", ""),
            )
            messages.success(
                request,
                followup_copy.moved_to(followups.local_label(followup.due_at, account)),
            )
        elif action == "reassign":
            from django.contrib.auth import get_user_model

            user_id = request.POST.get("user") or ""
            if user_id and not user_id.isdigit():
                raise ActionError("That person isn't on your team.")
            user = (
                get_user_model().objects.filter(pk=int(user_id)).first()
                if user_id
                else None
            )
            run_action("reassign_followup", ctx, followup=followup, user=user)
            messages.success(
                request,
                followup_copy.assigned_to(followup_copy.person_name(user))
                if user
                else followup_copy.ASSIGNED_TO_TEAM,
            )
        elif action == "edit":
            try:
                followups.edit(followup, note=request.POST.get("note", "").strip())
            except followups.FollowUpClosed as exc:
                raise ActionError(str(exc)) from exc
            messages.success(request, followup_copy.NOTE_UPDATED)
        elif action == "cancel":
            run_action(
                "complete_followup",
                ctx,
                followup=followup,
                by=request.user,
                reason=FollowUp.ClosedReason.CANCELLED,
            )
            messages.success(request, followup_copy.CANCELLED)
        elif action == "reopen":
            run_action("reopen_followup", ctx, followup=followup)
            messages.success(request, followup_copy.REOPENED)
    except ActionError as exc:
        messages.error(request, str(exc))
    return _back(request, "conversations:followups_due")


@login_required
def followups_badge(request):
    """The count of customers with a follow-up due for me (mine, or nobody's): the nav
    badge, the inbox header link and the "it's due" toast. ``followups.due_for``, the same
    number as the Mine tab, so the badge and the page never disagree."""
    from urllib.parse import quote

    from apps.conversations import followup_copy, followups

    account = get_current_account(request)
    if account is None:
        return HttpResponse("")
    due = followups.due_for(request.user, account)
    if request.GET.get("plain"):
        # The empty pane's "Follow-ups due" (_inbox_today.html): the number alone, 0 included.
        return HttpResponse(str(due))
    label = followup_copy.due_count(due)
    html = (
        f'<span class="inbox-tab-count inbox-tab-count--danger" '
        f'aria-label="{label}" title="{label}">{due}</span>'
        if due
        else ""
    )
    response = HttpResponse(html)
    if request.GET.get("toast") and due:
        response["X-Toast"] = "info|" + quote(f"{label}.")
    return response


def inbox_unread_for(user, account) -> int:
    """Open, unread conversations this person is expected to look at: assigned to them,
    or to nobody in particular, or to one of their teams with no one picked yet.

    Unread is one flag per conversation, shared by the team like a shared mailbox: when
    anyone opens it, it stops counting for everyone (Conversation.is_unread)."""
    team_ids = list(
        Team.objects.filter(account=account, members=user).values_list("id", flat=True)
    )
    mine = Q(assigned_to=user) | Q(assigned_to__isnull=True, assigned_team__isnull=True)
    if team_ids:
        mine |= Q(assigned_to__isnull=True, assigned_team_id__in=team_ids)
    return (
        hide_disabled_channels(
            Conversation.objects.filter(
                account=account, status=Conversation.Status.OPEN, is_unread=True
            )
        )
        .filter(mine)
        .count()
    )


@login_required
def inbox_badge(request):
    """The sidebar's Inbox count: unread conversations for me (inbox_unread_for) plus
    follow-ups due for me (the Follow-ups tab's number). One element on the page asks;
    every badge shown copies it (components/_nav_item.html, app.js syncBadges). ``data-due``
    carries the follow-ups due alone, for the inbox's "Follow-ups to do"."""
    from apps.conversations import followups

    account = get_current_account(request)
    if account is None:
        return HttpResponse("")
    unread = inbox_unread_for(request.user, account)
    due = followups.due_for(request.user, account)
    total = unread + due
    if not total:
        # Still says how many follow-ups are due (none), for the inbox's "Follow-ups to do".
        return HttpResponse('<span hidden data-due="0"></span>')
    parts = []
    if unread:
        parts.append(f"{unread} unread conversation{'s' if unread != 1 else ''}")
    if due:
        parts.append(f"{due} follow-up{'s' if due != 1 else ''} due")
    label = " and ".join(parts)
    tone = " inbox-tab-count--danger" if due else ""
    return HttpResponse(
        f'<span class="inbox-tab-count{tone}" data-due="{due}" aria-label="{label}" '
        f'title="{label}">{total if total < 100 else "99+"}</span>'
    )


@login_required
@require_POST
def followup_emails(request):
    """Turn "email me when follow-ups given to me are due" on or off for this business."""
    from apps.accounts.models import Membership

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    on = request.POST.get("on") == "1"
    Membership.objects.filter(account=account, user=request.user).update(
        followup_emails=on
    )
    from apps.conversations import followup_copy

    messages.success(
        request, followup_copy.EMAILS_ON if on else followup_copy.EMAILS_OFF
    )
    return redirect("conversations:followups_due")


def _back(request, default: str):
    next_url = request.POST.get("next") or ""
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        return redirect(next_url)
    return redirect(default)


def _add_overlay(
    request,
    partial: str,
    title: str,
    back_url: str,
    back_label: str,
    *,
    error: str = "",
    values: dict | None = None,
    status: int = 200,
):
    """An "Add …" form: in a modal over its list (``?add=1`` with data-overlay-open), or as
    its own page (components/_overlay_page.html). A failed save re-renders it with the
    error (422); a good one closes it (overlay_done) or goes on to what was made."""
    from apps.core.htmx import overlay_or_page

    return overlay_or_page(
        request,
        partial,
        "components/_overlay_page.html",
        {
            "overlay_partial": partial,
            "page_title": title,
            "back_url": back_url,
            "back_label": back_label,
            "error": error,
            "values": values or {},
        },
        kind="modal",
        title=title,
        status=status,
    )


@login_required
def saved_replies(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    from apps.core.htmx import is_overlay, overlay_done

    back = reverse("conversations:saved_replies")
    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        body = request.POST.get("body", "").strip()
        if not title or not body:
            error = "A saved reply needs both a title and a message."
            if is_overlay(request):
                return _add_overlay(
                    request,
                    "conversations/add/_saved_reply.html",
                    "Add a saved reply",
                    back,
                    "Saved replies",
                    error=error,
                    values={"title": title, "body": body},
                    status=422,
                )
            messages.error(request, error)
            return redirect(back)
        SavedReply.objects.create(account=account, title=title, body=body)
        return overlay_done(request, "Saved reply added.", back)
    if request.GET.get("add"):
        return _add_overlay(
            request,
            "conversations/add/_saved_reply.html",
            "Add a saved reply",
            back,
            "Saved replies",
        )
    replies = SavedReply.objects.filter(account=account).order_by("title")
    if is_background(request) and request.GET.get("menu"):
        # The composer's Saved replies menu, filled the first time it opens rather than
        # with every conversation (_conversation_pane.html).
        response = render(
            request, "conversations/_saved_reply_items.html", {"replies": replies}
        )
        response["Vary"] = "HX-Request"
        response["Cache-Control"] = "no-store"
        return response
    return render(
        request,
        "conversations/saved_replies.html",
        {"account": account, "replies": replies, "active_tab": "replies"},
    )


@login_required
@require_POST
def saved_reply_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    from apps.core.htmx import action_done
    from apps.core.undo import undo_url

    reply = get_object_or_404(SavedReply, account=account, pk=pk)
    # Kept for 30 days so the toast's Undo can bring it back (SavedReply.deleted_at).
    reply.deleted_at = timezone.now()
    reply.save(update_fields=["deleted_at"])
    return action_done(
        request,
        f"Saved reply “{reply.title}” deleted.",
        reverse("conversations:saved_replies"),
        undo=undo_url(request, "saved_reply_delete", reply.pk),
    )


@login_required
def teams(request):
    """B.2: organizational teams (Sales, Support, ...) that RoutingRules target.
    Deliberately minimal — a name and a member roster, no per-team settings."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    from apps.core.htmx import is_overlay, overlay_done

    # Teams and routing rules are one Inbox settings page (inbox_settings_teams).
    back = reverse("conversations:inbox_settings_teams") + "#teams"
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        error = ""
        if not name:
            error = "A team needs a name."
        elif Team.objects.filter(account=account, name=name).exists():
            error = "A team with that name already exists."
        if error:
            if is_overlay(request):
                return _add_overlay(
                    request,
                    "conversations/add/_team.html",
                    "Add a team",
                    back,
                    "Teams & routing",
                    error=error,
                    values={"name": name},
                    status=422,
                )
            messages.error(request, error)
            return redirect(back)
        Team.objects.create(account=account, name=name)
        return overlay_done(request, "Team added.", back)
    if request.GET.get("add"):
        return _add_overlay(
            request,
            "conversations/add/_team.html",
            "Add a team",
            back,
            "Teams & routing",
        )
    return redirect(back)


@login_required
def inbox_settings(request):
    """Inbox settings opens on its first tab, Saved replies."""
    return redirect("conversations:saved_replies")


@login_required
def inbox_settings_teams(request):
    """Inbox settings › Teams & routing: the teams, and the rules that send a new
    conversation to one, on one page (they only make sense together). The forms post to
    the teams and routing_rules views, which come back here."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    team_list = (
        Team.objects.filter(account=account)
        .annotate(member_count=Count("members"))
        .order_by("name")
    )
    rules = (
        RoutingRule.objects.filter(account=account)
        .select_related("team")
        .order_by("priority", "id")
    )
    return render(
        request,
        "conversations/inbox_settings_teams.html",
        {
            "account": account,
            "teams": team_list,
            "rules": rules,
            "channel_choices": visible_choices(Conversation.Channel.choices),
            "active_tab": "teams",
        },
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
            "active_tab": "teams",
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
    return redirect(reverse("conversations:inbox_settings_teams") + "#routing")


@login_required
@require_POST
def routing_rule_toggle(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    rule = get_object_or_404(RoutingRule, account=account, pk=pk)
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active"])
    return redirect(reverse("conversations:inbox_settings_teams") + "#routing")


@login_required
@require_POST
def routing_rule_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    rule = get_object_or_404(RoutingRule, account=account, pk=pk)
    rule.delete()
    messages.success(request, "Routing rule removed.")
    return redirect(reverse("conversations:inbox_settings_teams") + "#routing")


_FORM_FIELD_TYPES = list(_FLOW_FIELD_TYPES)
_FIELD_TYPE_LABELS = {
    "text": "Short answer",
    "long_text": "Long answer",
    "email": "Email address",
    "phone": "Phone number",
    "number": "Number",
    "choice": "One choice (buttons)",
    "dropdown": "One choice (drop-down)",
    "multi_choice": "Several choices",
    "date": "Date",
    "opt_in": "Yes / no agreement",
}
_MAPS_TO_LABELS = [
    ("", "Only keep it with the form's answers"),
    ("contact.first_name", "Customer's first name"),
    ("contact.last_name", "Customer's last name"),
    ("contact.email", "Customer's email (if we don't have one)"),
    ("contact.phone", "Customer's phone (if we don't have one)"),
]


@login_required
@module_required("automations")
def forms_list(request):
    """Forms a business sends in a conversation: a WhatsApp Flow, or questions one by one.
    Deterministic: no AI drafts or answers a form question."""
    from django.db.models import Count

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    from apps.core.htmx import is_overlay

    back = reverse("conversations:forms_list")
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if not name:
            if is_overlay(request):
                return _add_overlay(
                    request,
                    "conversations/add/_form.html",
                    "Add a form",
                    back,
                    "Forms",
                    error="A form needs a name.",
                    status=422,
                )
            messages.error(request, "A form needs a name.")
            return redirect(back)
        form = ConversationForm.objects.create(account=account, name=name[:150])
        detail = reverse("conversations:form_detail", args=[form.pk])
        if is_overlay(request):
            # From the modal: straight on to the new form's questions.
            response = HttpResponse(status=204)
            response["HX-Redirect"] = detail
            return response
        return redirect(detail)
    if request.GET.get("add"):
        return _add_overlay(
            request, "conversations/add/_form.html", "Add a form", back, "Forms"
        )
    forms_qs = (
        ConversationForm.objects.filter(account=account)
        .annotate(
            sent=Count("responses"),
            done=Count("responses", filter=Q(responses__status="completed")),
        )
        .order_by("name")
    )
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


def _clean_questions(raw) -> tuple[list, list]:
    """The builder's questions, tidied: ``(questions, problems)``. Keys stay stable for
    existing questions so answers already collected keep their names."""
    from apps.conversations.flow_json import CHOICE_TYPES

    questions: list[dict] = []
    problems: list[str] = []
    used: set[str] = set()
    if not isinstance(raw, list):
        return [], ["The questions didn't arrive. Try again."]
    for n, item in enumerate(raw[:50], 1):
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or "").split())[:200]
        field_type = item.get("field_type") or "text"
        if not label:
            problems.append(f"Question {n} needs some words.")
            continue
        if field_type not in _FORM_FIELD_TYPES:
            problems.append(f"Question {n} has an unknown answer type.")
            continue
        key = slugify(item.get("key") or "").replace("-", "_") or (
            slugify(label).replace("-", "_")[:40] or "question"
        )
        base, i = key, 2
        while key in used:
            key, i = f"{base}_{i}", i + 1
        used.add(key)
        maps_to = str(item.get("maps_to") or "")
        if maps_to and not (
            maps_to in {m for m, _ in _MAPS_TO_LABELS}
            or maps_to.startswith(("contact.attributes.", "lead.attributes."))
        ):
            maps_to = ""
        if maps_to.endswith("attributes."):
            maps_to = ""
        question = {
            "key": key,
            "label": label,
            "field_type": field_type,
            "maps_to": maps_to,
            "required": bool(item.get("required", True)),
        }
        helper = " ".join(str(item.get("helper") or "").split())[:200]
        if helper:
            question["helper"] = helper
        if field_type in CHOICE_TYPES:
            question["options"] = [
                " ".join(str(o).split())[:60]
                for o in (item.get("options") or [])
                if str(o).strip()
            ][:200]
        if item.get("page_break") and questions:
            question["page_break"] = True
        questions.append(question)
    return questions, problems


@login_required
@module_required("automations")
def form_detail(request, pk):
    from apps.conversations.flow_json import check as check_flow

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
            from apps.whatsapp.tasks import publish_conversation_flow_task

            problems = check_flow(
                form.questions, title=form.screen_title, submit=form.submit_label
            )
            if problems:
                messages.error(request, f"Fix this first: {problems[0]}")
                return redirect("conversations:form_detail", pk=form.pk)
            form.flow_status = ConversationForm.FlowStatus.PUBLISHING
            form.flow_error = ""
            form.save(update_fields=["flow_status", "flow_error", "updated_at"])
            publish_conversation_flow_task.delay(form.pk)
            form.refresh_from_db()
            if form.flow_status == ConversationForm.FlowStatus.PUBLISHED:
                messages.success(request, "WhatsApp Flow published successfully.")
            elif form.flow_status == ConversationForm.FlowStatus.ERROR:
                messages.error(
                    request, f"Publish failed: {form.flow_error or 'unknown error'}"
                )
            else:
                messages.info(
                    request, "Publishing to WhatsApp. This takes a few seconds."
                )
        elif action == "set_status":
            status = request.POST.get("status", "")
            if status == ConversationForm.Status.PUBLISHED and not form.questions:
                messages.error(request, "Add at least one question before publishing.")
            elif status in ConversationForm.Status.values:
                form.status = status
                form.save(update_fields=["status", "updated_at"])
        return redirect("conversations:form_detail", pk=form.pk)

    from apps.contacts.models import CustomAttributeDef

    attribute_targets = [
        (
            f"{d.entity}.attributes.{d.key}",
            f"{'Lead' if d.entity == 'lead' else 'Customer'} field: {d.label or d.key}",
        )
        for d in CustomAttributeDef.objects.filter(
            account=account, archived_at__isnull=True
        ).order_by("entity", "key")
        if d.entity in ("contact", "lead")
    ]
    responses = form.responses.all()
    sent = responses.count()
    done = responses.filter(status=FormResponse.Status.COMPLETED).count()
    stats = {
        "sent": sent,
        "done": done,
        "waiting": responses.filter(status=FormResponse.Status.IN_PROGRESS).count(),
        "rate": round(done / sent * 100) if sent else None,
    }
    return render(
        request,
        "conversations/form_detail.html",
        {
            "account": account,
            "form": form,
            "field_types": _FORM_FIELD_TYPES,
            "builder": {
                "questions": form.questions,
                "copy": {
                    "intro_text": form.intro_text,
                    "cta_label": form.cta_label,
                    "screen_title": form.screen_title,
                    "submit_label": form.submit_label,
                    "thanks_text": form.thanks_text,
                    "timeout_hours": form.timeout_hours,
                    "lead_action": form.lead_action,
                },
                "fieldTypes": [
                    {"value": t, "label": _FIELD_TYPE_LABELS.get(t, t)}
                    for t in _FORM_FIELD_TYPES
                ],
                "mapsTo": [{"value": v, "label": label} for v, label in _MAPS_TO_LABELS]
                + [{"value": v, "label": label} for v, label in attribute_targets],
                "name": form.name,
            },
            "flow_problems": check_flow(
                form.questions, title=form.screen_title, submit=form.submit_label
            )
            if form.presentation == ConversationForm.Presentation.WHATSAPP_FLOW
            else [],
            "stats": stats,
            "recent": responses.select_related("contact").order_by("-started_at")[:10],
        },
    )


@login_required
@module_required("automations")
@require_POST
def form_save(request, pk):
    """The builder's autosave: questions and the words the customer sees."""
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)
    form = get_object_or_404(ConversationForm, account=account, pk=pk)
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    questions, problems = _clean_questions(body.get("questions"))
    copy = body.get("copy") or {}
    fields = ["questions", "updated_at"]
    form.questions = questions
    if isinstance(body.get("name"), str) and body["name"].strip():
        form.name = body["name"].strip()[:150]
        fields.append("name")
    for name, limit in (
        ("intro_text", 500),
        ("cta_label", 20),
        ("screen_title", 30),
        ("submit_label", 35),
        ("thanks_text", 500),
    ):
        if name in copy:
            setattr(form, name, " ".join(str(copy[name] or "").split())[:limit])
            fields.append(name)
    if "timeout_hours" in copy:
        try:
            form.timeout_hours = max(1, min(24 * 30, int(copy["timeout_hours"])))
            fields.append("timeout_hours")
        except (TypeError, ValueError):
            problems.append("The time limit must be a number of hours.")
    if copy.get("lead_action", "") in ConversationForm.LeadAction.values:
        form.lead_action = copy.get("lead_action", "")
        fields.append("lead_action")
    form.save(update_fields=fields)
    form.note_questions_changed()
    from apps.conversations.flow_json import check as check_flow

    flow_problems = (
        check_flow(form.questions, title=form.screen_title, submit=form.submit_label)
        if form.presentation == ConversationForm.Presentation.WHATSAPP_FLOW
        else []
    )
    form.refresh_from_db(fields=["flow_status"])
    return JsonResponse(
        {
            "ok": True,
            "questions": form.questions,
            "problems": problems,
            "flow_problems": flow_problems,
            "flow_status": form.flow_status,
        }
    )


@login_required
@module_required("automations")
def form_flow_status(request, pk):
    """The Flow status card, polled while publishing."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    form = get_object_or_404(ConversationForm, account=account, pk=pk)
    # oob: also refresh the header badge, which shows the same status.
    return render(
        request, "conversations/_form_flow_status.html", {"form": form, "oob": True}
    )


@login_required
@module_required("automations")
def form_responses(request, pk):
    """Everyone who got this form and what they answered; ``?format=csv`` to download."""
    import csv

    from django.core.paginator import Paginator
    from django.http import HttpResponse

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    form = get_object_or_404(ConversationForm, account=account, pk=pk)
    rows = form.responses.select_related("contact", "conversation").order_by(
        "-started_at"
    )
    status = request.GET.get("status", "")
    if status in FormResponse.Status.values:
        rows = rows.filter(status=status)
    else:
        status = ""  # an unknown filter shows everything, and the page says so
    keys = [q["key"] for q in form.questions]

    def cell(value):
        return (
            ", ".join(value)
            if isinstance(value, list)
            else ("" if value is None else str(value))
        )

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = (
            f'attachment; filename="{slugify(form.name) or "form"}-answers.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(
            ["customer", "phone", "status", "started", "finished"]
            + [q["label"] for q in form.questions]
        )
        for r in rows.iterator():
            writer.writerow(
                [
                    r.contact.display_name,
                    r.contact.phone or "",
                    r.get_status_display(),
                    r.started_at.isoformat(),
                    r.completed_at.isoformat() if r.completed_at else "",
                ]
                + [cell(r.answers.get(k)) for k in keys]
            )
        return response
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    for r in page:
        r.cells = [cell(r.answers.get(k)) for k in keys]  # type: ignore[attr-defined]
    return render(
        request,
        "conversations/form_responses.html",
        {"account": account, "form": form, "page": page, "status": status},
    )


@login_required
@module_required("automations")
@require_POST
def form_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    form = get_object_or_404(ConversationForm, account=account, pk=pk)
    if form.flow_id:
        try:
            from apps.whatsapp.providers import get_whatsapp_provider

            get_whatsapp_provider(account).deprecate_flow(form.flow_id)
        except Exception:
            logger.info("form %s: couldn't retire its Flow on delete", form.pk)
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
    conversation = get_visible_conversation(account, public_id)
    upload = request.FILES.get("file")
    if upload is None:
        return JsonResponse(
            {"ok": False, "error": "Choose a file to send."}, status=400
        )
    channel = conversation.channel
    if channel not in (
        outbound_media.WHATSAPP,
        outbound_media.INSTAGRAM,
        outbound_media.FACEBOOK,
    ):
        return JsonResponse(
            {"ok": False, "error": "Media can't be sent on this channel."}, status=400
        )
    key = inbox_key(conversation, request.user, request.POST.get("client_key"))
    try:
        # A repeat of a send already made with this key needs no new file: the action
        # answers with what the first attempt did. Preparing it would store a copy that
        # nothing ever uses.
        media = (
            None
            if inbox_send_started(conversation, key)
            else outbound_media.prepare(
                upload,
                channel,
                account_id=account.pk,
                voice=request.POST.get("voice") == "1",
            )
        )
        result = run_action(
            "reply_media",
            {"account": account},
            conversation=conversation,
            media=media,
            caption=(request.POST.get("caption") or "").strip(),
            idempotency_key=key,
        )
    except (outbound_media.MediaError, ActionError) as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=400)
    conversation.mark_read()
    return JsonResponse({"ok": True, **result})


def _recent_media(conversation) -> dict:
    """``{message_id: media_json}`` for the latest inbound messages that carry media."""
    recent = (
        conversation.messages.filter(direction=Message.Direction.INBOUND)
        .select_related("whatsapp_message", "instagram_message", "facebook_message")
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
    conversation = get_visible_conversation(account, public_id)
    message = get_object_or_404(
        Message.objects.select_related(
            "whatsapp_message", "instagram_message", "facebook_message"
        ),
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
    conversation = get_visible_conversation(account, public_id)

    if "before" in request.GET:
        return _earlier_messages(conversation, request.GET.get("before"))

    try:
        after = max(int(request.GET.get("after") or 0), 0)
    except ValueError:
        after = 0

    new = list(
        conversation.messages.select_related(
            "whatsapp_message", "instagram_message", "facebook_message"
        )
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

    from apps.conversations import presence

    # Support looking "as" the business is not a teammate in the conversation.
    if viewing_as(request) is None:
        presence.mark_viewing(conversation.pk, request.user.pk)
    others = presence.for_conversation(
        conversation.pk, list(_team_members(account)), exclude_user_id=request.user.pk
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
                    "failureReason": _failure(m.metadata)[0],
                    "failureFix": _failure(m.metadata)[1],
                    "byAi": (m.metadata or {}).get("sent_by") == "ai",
                    "clientKey": (m.metadata or {}).get("client_key") or None,
                    "media": media_json(m, conversation.public_id),
                }
                for m in new
            ],
            "statuses": {str(m.id): m.status for m in recent_outbound},
            # Media that was still downloading when first shown: its current state,
            # so a photo or voice note appears without reloading the page.
            "media": _recent_media(conversation),
            "failureReasons": {
                str(m.id): reason
                for m in recent_outbound
                if (reason := _failure(m.metadata)[0])
            },
            "failureFixes": {
                str(m.id): fix
                for m in recent_outbound
                if (fix := _failure(m.metadata)[1])
            },
            "windowOpen": _window_is_open(conversation),
            "aiProposal": _ai_proposal_json(account, conversation),
            "status_html": render_to_string(
                "conversations/_status_badges.html",
                {
                    "conversation": conversation,
                    "snapshot": state_of(conversation),
                    "now": timezone.now(),
                    "WAITING_FOR_AGENT": ConversationState.WAITING_FOR_AGENT,
                    "WAITING_FOR_CUSTOMER": ConversationState.WAITING_FOR_CUSTOMER,
                },
            ),
            "open": conversation.status == Conversation.Status.OPEN,
            "presence": others,
            # Read last, so it is as close as it can be to when the answer leaves: the
            # browser times its own clock against it (chat.js _skew).
            "serverNow": timezone.now().isoformat(),
        }
    )


def _earlier_messages(conversation, before) -> JsonResponse:
    """``?before=<id>``: the THREAD_WINDOW messages just older than that one, oldest first
    ("Show earlier messages"). Reading history never marks anything read."""
    anchor = (
        conversation.messages.filter(pk=before).values("timestamp", "id").first()
        if str(before or "").isdigit()
        else None
    )
    if anchor is None:
        return JsonResponse({"messages": [], "hasEarlier": False})
    older = list(
        conversation.messages.select_related(
            "whatsapp_message", "instagram_message", "facebook_message"
        )
        .filter(
            Q(timestamp__lt=anchor["timestamp"])
            | Q(timestamp=anchor["timestamp"], id__lt=anchor["id"])
        )
        .order_by("-timestamp", "-id")[: THREAD_WINDOW + 1]
    )
    return JsonResponse(
        {
            "messages": [
                _message_json(m, conversation) for m in older[:THREAD_WINDOW][::-1]
            ],
            "hasEarlier": len(older) > THREAD_WINDOW,
        }
    )


# Channels whose API can show the customer that we're typing. Neither tells us when the
# customer is typing: Meta's webhooks carry messages and receipts, never typing.
_CUSTOMER_TYPING_CHANNELS = {
    Conversation.Channel.WHATSAPP,
    Conversation.Channel.INSTAGRAM,
    Conversation.Channel.FACEBOOK,
}
_CUSTOMER_TYPING_EVERY = 20  # seconds between indicators sent to Meta, per conversation


@login_required
@require_POST
def conversation_typing(request, public_id: str):
    """The composer's "someone is writing a reply" ping (static/js/chat.js pingTyping).

    Marks the teammate as typing for everyone else in the conversation, and — at most every
    20 seconds, on an open conversation inside the 24-hour window — shows the customer a
    typing indicator on WhatsApp or Instagram. Best effort: the answer is always ``ok``.
    """
    from django.core.cache import cache

    from apps.conversations import presence

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"ok": False, "error": "no account"}, status=403)
    conversation = get_visible_conversation(account, public_id)
    if viewing_as(request) is not None:
        return JsonResponse(
            {"ok": True}
        )  # support viewing "as" the business doesn't type

    presence.mark_typing(conversation.pk, request.user.pk)

    if (
        conversation.channel in _CUSTOMER_TYPING_CHANNELS
        and conversation.status == Conversation.Status.OPEN
        and _window_is_open(conversation)
        and cache.add(
            f"typing-out:{conversation.pk}", 1, _CUSTOMER_TYPING_EVERY
        )  # True only for the first ping in the window
    ):
        from apps.conversations.tasks import send_typing_indicator

        try:
            send_typing_indicator.delay(conversation.pk)
        except Exception:
            logger.debug("typing indicator not queued for %s", conversation.pk)

    if getattr(settings, "REALTIME_SSE_ENABLED", False):
        from apps.core import realtime

        # The open panes already re-poll on "message.created" for their own conversation
        # (chat.js), and that poll carries the presence: no new event wiring needed.
        realtime.publish(
            account.pk,
            "message.created",
            {"conversation_id": conversation.public_id},
        )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def create_ticket_from_conversation(request, public_id: str):
    """Create a support ticket from an agent inbox conversation.

    POST params:
      priority   — p1/p2/p3/p4 (optional, default p3)
      subject    — override the derived subject (optional)
      force      — "1" to create even if an open ticket already exists
    """
    from apps.core.releases import is_released

    if not is_released("support"):
        raise Http404("Support tickets are not available yet.")
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
    conversation = get_object_or_404(
        hide_disabled_channels(Conversation.objects.all()),
        pk=conversation_id,
        account=account,
    )

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
