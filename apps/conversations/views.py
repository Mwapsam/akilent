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
    Case,
    Count,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    When,
)
from django.http import HttpResponse, JsonResponse
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
    get_conversation_state,
    missed,
    needs_attention,
    snapshot_of,
    unanswered_q,
    with_activity,
)
from apps.core.channels import hide_disabled_channels, visible_choices
from apps.core.htmx import is_background
from apps.core.module_gate import module_required

if TYPE_CHECKING:
    from django.http.response import HttpResponseBase

logger = logging.getLogger(__name__)

_PAGE_SIZE = 30


def _last_handled_by(conversation) -> str:
    """Who took the customer's latest message, in words (see services.HANDLED_LABELS)."""
    from apps.conversations.services import HANDLED_LABELS

    last = (
        Message.objects.filter(
            conversation=conversation, direction=Message.Direction.INBOUND
        )
        .order_by("-timestamp", "-id")
        .values_list("metadata", flat=True)
        .first()
    )
    if not last or "handled_by" not in last:
        return ""
    return HANDLED_LABELS.get(last["handled_by"], "")


def get_visible_conversation(account, public_id):
    """A conversation the inbox may show: 404 for one on a channel the admin switched off."""
    return get_object_or_404(
        hide_disabled_channels(Conversation.objects.all()),
        account=account,
        public_id=public_id,
    )


def _inbox_context(request, account) -> dict:
    now = timezone.now()
    view = (request.GET.get("view") or "all").strip()
    _open_followups = FollowUp.objects.filter(
        conversation=OuterRef("pk"), done_at__isnull=True
    ).order_by("due_at")
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
    elif view == "followups":
        # Conversations with a reminder still open, soonest due first.
        qs = (
            with_activity(Conversation.objects.filter(account=account))
            .filter(Exists(_open_followups))
            .annotate(next_due=Subquery(_open_followups.values("due_at")[:1]))
            .order_by("next_due", "-id")
        )
    else:
        view = "all"
        qs = with_activity(Conversation.objects.filter(account=account)).order_by(
            "-last_any", "-id"
        )
    # Conversations on a channel the admin switched off are hidden from every list.
    qs = hide_disabled_channels(qs.select_related("contact"))

    channel = (request.GET.get("channel") or "").strip()
    if channel:
        qs = qs.filter(channel=channel)

    # Combinable with any tab ("My + Overdue", "Unassigned + Overdue"): the
    # same waiting-on-us definition every tab already uses, just past the
    # stricter OVERDUE_WAITING cutoff instead of the 24h one.
    overdue_only = bool(request.GET.get("overdue"))
    if overdue_only:
        qs = qs.filter(unanswered_q(), last_in__lte=now - OVERDUE_WAITING)

    # The row preview is the latest message, annotated here rather than fetched per row:
    # the list is re-rendered by the 8-second poll for every open inbox.
    latest = Message.objects.filter(conversation=OuterRef("pk")).order_by(
        "-timestamp", "-id"
    )
    qs = qs.annotate(
        preview_body=Subquery(latest.values("body")[:1]),
        preview_direction=Subquery(latest.values("direction")[:1]),
        has_followup=Exists(_open_followups),
    )

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
        c.has_messages = c.preview_direction is not None

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

    return {
        "account": account,
        "now": now,
        "page": page,
        "view": view,
        "channel": channel,
        "channel_choices": visible_choices(Conversation.Channel.choices),
        "overdue_only": overdue_only,
        "needs_attention_count": hide_disabled_channels(
            needs_attention(account, now)
        ).count(),
        "missed_count": hide_disabled_channels(missed(account, now)).count(),
        "assigned_count": hide_disabled_channels(
            Conversation.objects.filter(account=account, assigned_to=request.user)
        ).count(),
        "unassigned_count": hide_disabled_channels(
            Conversation.objects.filter(
                account=account,
                status=Conversation.Status.OPEN,
                assigned_to__isnull=True,
            )
        ).count(),
        "closed_count": hide_disabled_channels(
            Conversation.objects.filter(
                account=account, status=Conversation.Status.CLOSED
            )
        ).count(),
        "all_count": hide_disabled_channels(
            Conversation.objects.filter(account=account)
        ).count(),
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
    return JsonResponse(
        {
            "ok": True,
            "message": message,
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
                if in_pane:
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
                assignee = _resolve_assignee(account, request)
                run_action(
                    "assign_conversation",
                    ctx,
                    conversation=conversation,
                    user=assignee,
                    assigned_by=request.user,
                )
                if in_pane:
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
                if in_pane:
                    messages.success(request, "Note added.")
            elif action == "mark_read":
                conversation.mark_read()
            elif action == "close":
                resolution = request.POST.get("resolution", "")
                if resolution and resolution not in Conversation.Resolution.values:
                    raise ActionError("That's not a valid close reason.")
                conversation.close(
                    resolution=resolution, actor=f"user:{request.user.pk}"
                )
                if in_pane:
                    messages.success(
                        request, "Closed. It reopens if the customer writes again."
                    )
            elif action == "reopen":
                conversation.reopen(actor=f"user:{request.user.pk}")
                if in_pane:
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
                if in_pane:
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
                        else f" for {hours:g} hours."
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
        if not in_pane:
            return redirect("conversations:detail", public_id=public_id)
        # In the pane: re-render it below, from fresh state, instead of redirecting away.
        conversation = get_visible_conversation(account, public_id)

    if (
        viewing_as(request) is None
    ):  # support looking "as" the business mustn't clear its unread
        conversation.mark_read()
    thread = list(
        conversation.messages.select_related(
            "whatsapp_message", "instagram_message", "facebook_message"
        )
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
        "typingUrl": reverse("conversations:typing", args=[conversation.public_id]),
        "lastId": thread[-1].id if thread else 0,
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
            {
                "id": m.id,
                "direction": m.direction,
                "body": m.body,
                "ts": m.timestamp.isoformat(),
                "status": m.status,
                "failureReason": (m.metadata or {}).get("failure_reason") or None,
                "byAi": (m.metadata or {}).get("sent_by") == "ai",
                # The browser's key for a reply sent from the inbox (inbox_keys), so its
                # "Sending…" or "Not confirmed" bubble is matched to exactly this message.
                "clientKey": (m.metadata or {}).get("client_key") or None,
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
    from apps.conversations import followups as followup_service

    # This conversation's own reminder first, else one for the same customer elsewhere.
    open_followup = (
        FollowUp.objects.filter(
            account=account, contact=conversation.contact, done_at__isnull=True
        )
        .select_related("assigned_to")
        .annotate(
            elsewhere=Case(
                When(conversation=conversation, then=0),
                default=1,
                output_field=IntegerField(),
            )
        )
        .order_by("elsewhere", "due_at")
        .first()
    )
    followup_still_needed = (
        open_followup is not None
        and open_followup in followup_service.open_after_customer_replied(conversation)
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
        if active_form_response is None
        else []
    )

    active_recommendation = _active_recommendation_for(conversation)

    pane_context = {
        "account": account,
        "now": now,
        "automation_activity": automation_activity,
        "automation_now": automation_now,
        "last_handled_by": _last_handled_by(conversation),
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
        "open_followup_due": followup_service.local_label(open_followup.due_at, account)
        if open_followup
        else "",
        "followup_still_needed": followup_still_needed,
        "snooze_choices": [c for c in followup_service.CHOICES if c[0] != "custom"],
        "team_members": _team_members(account),
        "active_form_response": active_form_response,
        "published_forms": published_forms,
        "can_delete": _can_delete(request, account),
        "in_pane": in_pane,
        "detail_url": reverse("conversations:detail", args=[conversation.public_id]),
    }
    # The inbox list (templates/conversations/inbox.html, ≥lg) loads a conversation into its
    # own pane via a plain background GET — see static/js/inbox_pane.js — rather than a full
    # navigation, so opening one never re-loads the shell or the list beside it. Below lg there
    # is no pane to load into, so a row's own href still points here and gets the full page below.
    if in_pane:
        pane = render(request, "conversations/_conversation_pane.html", pane_context)
        _messages_to_toast(request, pane)
        if mutated:
            # Assignment, close and reopen change the list beside the pane: refresh it now.
            pane["HX-Trigger"] = "inbox-refresh"
        return pane

    # A full page load of a conversation's own address (a reload after opening it in the
    # pane, a bookmark, a link, a new tab) shows it the way it was opened: inside the inbox,
    # in the pane beside the list. Below lg the pane shows alone (.has-conversation), with
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


_TOAST_TYPES = {"error": "danger", "warning": "warning", "info": "info"}


def _messages_to_toast(request, response) -> None:
    """A pane swap renders no page shell, so queued flash messages would surface on some
    later page. Carry the latest one as an X-Toast instead (static/js/app.js shows it)."""
    from apps.accounts.utils import hx_toast

    last = None
    for message in messages.get_messages(request):
        last = message
    if last is not None:
        hx_toast(response, _TOAST_TYPES.get(last.level_tag, "success"), str(last))


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
    conversation = get_visible_conversation(account, public_id)
    if not _can_delete(request, account):
        messages.error(request, "Only owners and admins can delete conversations.")
        return redirect("conversations:detail", public_id=public_id)
    delete_conversation(conversation, actor=request.user)
    messages.success(request, "Conversation deleted.")
    return redirect("conversations:inbox")


@login_required
def followups_due(request):
    """The team's reminders: overdue, today and coming up, for "mine" or the whole team."""
    from datetime import timedelta

    from django.core.paginator import Paginator

    from apps.accounts.models import Membership
    from apps.conversations import followups
    from apps.core.channels import hide_disabled_channels

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    now = timezone.now()
    which = request.GET.get("list") or "mine"
    base = hide_disabled_channels(
        FollowUp.objects.filter(account=account, done_at__isnull=True),
        field="conversation__channel",
    ).select_related("contact", "conversation", "assigned_to")
    mine = base.filter(Q(assigned_to=request.user) | Q(assigned_to__isnull=True))
    scoped = mine if which == "mine" else base
    zone = followups.business_zone(account)
    local_now = now.astimezone(zone)
    end_of_today = (local_now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    due = Paginator(scoped.filter(due_at__lte=now).order_by("due_at"), 50).get_page(
        request.GET.get("page")
    )
    today = scoped.filter(due_at__gt=now, due_at__lt=end_of_today).order_by("due_at")
    upcoming = scoped.filter(due_at__gte=end_of_today).order_by("due_at")[:30]
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
            "now": now,
            "list": which,
            "due": due,
            "today": today,
            "upcoming": upcoming,
            "mine_count": mine.filter(due_at__lte=now).count(),
            "team_count": base.filter(due_at__lte=now).count(),
            "teammates": teammates,
            "emails_on": bool(membership and membership.followup_emails),
            "zone_name": local_now.tzname(),
            "snooze_choices": [c for c in followups.CHOICES if c[0] != "custom"],
        },
    )


@login_required
@require_POST
def followup_complete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    followup = get_object_or_404(FollowUp, account=account, pk=pk)
    run_action(
        "complete_followup", {"account": account}, followup=followup, by=request.user
    )
    messages.success(request, "Follow-up marked done.")
    return _back(request, "conversations:followups_due")


@login_required
@require_POST
def followup_action(request, pk, action):
    """Snooze, reassign, edit, cancel or reopen one reminder."""
    from apps.conversations import followups

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
                f"Snoozed until {followups.local_label(followup.due_at, account)}.",
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
                f"Now on {user.get_full_name() or user.email}'s list."
                if user
                else "Now on the whole team's list.",
            )
        elif action == "edit":
            followups.edit(followup, note=request.POST.get("note", "").strip())
            messages.success(request, "Reminder updated.")
        elif action == "cancel":
            run_action(
                "complete_followup",
                ctx,
                followup=followup,
                by=request.user,
                reason=FollowUp.ClosedReason.CANCELLED,
            )
            messages.success(request, "Reminder removed.")
        elif action == "reopen":
            run_action("reopen_followup", ctx, followup=followup)
            messages.success(request, "Reminder is back on the list.")
    except ActionError as exc:
        messages.error(request, str(exc))
    return _back(request, "conversations:followups_due")


@login_required
def followups_badge(request):
    """The nav's count of reminders due for me (mine, or nobody's), and the "it's due" toast."""
    from urllib.parse import quote

    from django.http import HttpResponse

    account = get_current_account(request)
    if account is None:
        return HttpResponse("")
    due = FollowUp.objects.filter(
        Q(assigned_to=request.user) | Q(assigned_to__isnull=True),
        account=account,
        done_at__isnull=True,
        due_at__lte=timezone.now(),
    ).count()
    html = (
        f'<span class="inbox-tab-count inbox-tab-count--danger" '
        f'aria-label="{due} due">{due}</span>'
        if due
        else ""
    )
    response = HttpResponse(html)
    if request.GET.get("toast") and due:
        response["X-Toast"] = "info|" + quote(
            f"You have {due} follow-up{'s' if due != 1 else ''} due."
        )
    return response


@login_required
@require_POST
def followup_emails(request):
    """Turn "email me when my reminders are due" on or off for the current business."""
    from apps.accounts.models import Membership

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    on = request.POST.get("on") == "1"
    Membership.objects.filter(account=account, user=request.user).update(
        followup_emails=on
    )
    messages.success(
        request,
        "We'll email you when your reminders are due."
        if on
        else "No more reminder emails. They still show here and in the bell.",
    )
    return redirect("conversations:followups_due")


def _back(request, default: str):
    next_url = request.POST.get("next") or ""
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        return redirect(next_url)
    return redirect(default)


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
            "channel_choices": visible_choices(Conversation.Channel.choices),
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
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "A form needs a name.")
            return redirect("conversations:forms_list")
        form = ConversationForm.objects.create(account=account, name=name[:150])
        return redirect("conversations:form_detail", pk=form.pk)
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
    return render(request, "conversations/_form_flow_status.html", {"form": form})


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
                    "failureReason": (m.metadata or {}).get("failure_reason") or None,
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
            "presence": others,
            # Read last, so it is as close as it can be to when the answer leaves: the
            # browser times its own clock against it (chat.js _skew).
            "serverNow": timezone.now().isoformat(),
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
