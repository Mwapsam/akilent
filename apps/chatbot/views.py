from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.chatbot import starters
from apps.chatbot.models import ChatbotConfig
from apps.chatbot.models.config import WEBSITE_CHAT_TRIGGERS
from apps.chatbot.services import chatbot as chatbot_service
from apps.core.htmx import full_page_load

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)


def _bot(request: HttpRequest, pk: int) -> ChatbotConfig:
    return get_object_or_404(
        ChatbotConfig.objects.select_related("workflow"),
        pk=pk,
        account=get_current_account(request),
        chatbot_type=ChatbotConfig.ChatbotType.CUSTOMER,
    )


def _is_admin(request: HttpRequest, account) -> bool:
    """Turning a bot's conversation on changes what every visitor gets: owners and admins only,
    the same rule as automations."""
    from apps.accounts.api import is_account_admin

    return request.user.is_superuser or is_account_admin(request.user, account)


@login_required
def chatbot_list(request: HttpRequest) -> HttpResponse:
    from apps.chatbot.models import ChatSession
    from apps.crm.models import Lead

    account = get_current_account(request)
    bots = list(
        ChatbotConfig.objects.filter(
            account=account, chatbot_type=ChatbotConfig.ChatbotType.CUSTOMER
        )
        .select_related("workflow")
        .order_by("-created_at")
    )
    week_ago = timezone.now() - timedelta(days=7)
    for bot in bots:
        # A chat = a visitor who wrote (or was greeted); just opening the page isn't one.
        bot.chats_this_week = ChatSession.objects.filter(
            chatbot=bot, started_at__gte=week_ago, conversation__isnull=False
        ).count()
        bot.leads_this_week = Lead.objects.filter(
            account=account,
            conversation__chat_session__chatbot=bot,
            created_at__gte=week_ago,
        ).count()
        bot.setup = _setup(bot)
    return render(request, "chatbot/list.html", {"bots": bots})


@login_required
def chatbot_create(request: HttpRequest) -> HttpResponse:
    account = get_current_account(request)
    context = {
        "starters": starters.list_starters(),
        "default_starter": starters.DEFAULT_STARTER,
    }
    if request.method != "POST":
        return render(request, "chatbot/create.html", context)

    name = request.POST.get("name", "").strip()
    starter_key = request.POST.get("starter", "")
    website = request.POST.get("website", "").strip()
    error = ""
    if not name:
        error = "Give your chatbot a name."
    elif starter_key not in starters.STARTERS:
        error = "Choose how the chatbot should start."
    if error:
        return render(
            request,
            "chatbot/create.html",
            {**context, "error": error, "post": request.POST},
        )

    from django.db import transaction

    try:
        with transaction.atomic():
            # chatbot_type and account are set server-side; submitted values are ignored.
            bot = chatbot_service.create_customer_chatbot(
                account,
                name=name[:100],
                welcome_message=starters.STARTERS[starter_key]["welcome"],
                allowed_domains=[website] if website else [],
            )
            starters.install(bot, starter_key)
    except ValidationError as exc:
        detail = "; ".join(getattr(exc, "messages", None) or [str(exc)])
        return render(
            request,
            "chatbot/create.html",
            {**context, "error": detail, "post": request.POST},
        )
    messages.success(
        request,
        f"“{bot.name}” is ready to try. Preview it, then turn it on when you're happy.",
    )
    return redirect("chatbot:detail", pk=bot.pk)


@login_required
def chatbot_detail(request: HttpRequest, pk: int) -> HttpResponse:
    from django.conf import settings

    from apps.ai import api as ai_api
    from apps.chatbot.services import install
    from apps.chatbot.services.ai_budget import used_today

    account = get_current_account(request)
    bot = _bot(request, pk)
    return render(
        request,
        "chatbot/detail.html",
        {
            "bot": bot,
            "embed_snippet": _embed_snippet(bot),
            "setup": _setup(bot),
            "install": install.status(bot),
            "can_publish": _is_admin(request, account),
            # How it answers: AI from the business's Knowledge, within the bot's daily limit.
            "ai_unavailable": ai_api.unavailable_reason(account),
            "ai_autonomy": getattr(settings, "AI_AUTONOMY_ENABLED", True),
            "ai_used_today": min(used_today(bot.pk), bot.ai_daily_limit),
        },
    )


@login_required
@require_POST
def chatbot_publish(request: HttpRequest, pk: int) -> HttpResponse:
    """Turn the bot's conversation on (or put its saved changes live)."""
    from apps.automation import api as automation_api

    account = get_current_account(request)
    bot = _bot(request, pk)
    if not _is_admin(request, account):
        messages.error(
            request, "Only an owner or admin can turn a chatbot on. Ask one of them."
        )
        return redirect("chatbot:detail", pk=bot.pk)
    if bot.workflow is None:
        messages.error(request, "Choose a conversation for this chatbot first.")
        return redirect("chatbot:detail", pk=bot.pk)
    try:
        automation_api.publish(bot.workflow)
    except (
        automation_api.WorkflowNotReady,
        automation_api.AutomationLimitReached,
    ) as exc:
        messages.error(request, f"Fix this first: {exc}")
        return redirect("chatbot:detail", pk=bot.pk)
    messages.success(
        request,
        "Your chatbot's conversation is on. Visitors already chatting finish the way they "
        "started.",
    )
    return redirect("chatbot:detail", pk=bot.pk)


@login_required
@full_page_load  # its own script drives the preview
def chatbot_preview(request: HttpRequest, pk: int) -> HttpResponse:
    from apps.automation import api as automation_api

    bot = _bot(request, pk)
    trigger = ""
    if bot.workflow is not None:
        definition = automation_api.editable_definition(bot.workflow)
        trigger = (definition.get("trigger") or {}).get("type", "")
    return render(
        request,
        "chatbot/preview.html",
        {
            "bot": bot,
            # Rendered with json_script, which escapes it for the page.
            "preview_cfg": {
                "url": reverse("chatbot:preview-run", args=[bot.pk]),
                "name": bot.name,
                "welcome": bot.welcome_message,
                "color": bot.primary_color,
                "has_flow": bot.workflow is not None,
                "greets": trigger == "website_chat.opened",
            },
        },
    )


@login_required
@require_POST
def chatbot_preview_run(request: HttpRequest, pk: int) -> JsonResponse:
    """Play the bot's saved conversation (unpublished changes included) for the preview.

    The automation simulator: it sends, saves and charges nothing. ``message`` is what started
    this run (empty for a bot that greets on open) and ``replies`` the visitor's answers since.
    """
    from apps.automation import api as automation_api
    from apps.automation import simulate

    bot = _bot(request, pk)
    if bot.workflow is None:
        return JsonResponse({"error": "no conversation"}, status=400)
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "invalid JSON"}, status=400)
    replies = [str(r)[:1000] for r in (body.get("replies") or [])][:20]
    return JsonResponse(
        simulate.simulate(
            bot.account,
            automation_api.editable_definition(bot.workflow),
            message=str(body.get("message") or "")[:1000],
            replies=replies,
        )
    )


@login_required
def chatbot_edit(request: HttpRequest, pk: int) -> HttpResponse:
    account = get_current_account(request)
    bot = _bot(request, pk)
    workflows = _workflow_choices(bot)

    if request.method == "POST":
        fields = {
            "name": request.POST.get("name", bot.name).strip() or bot.name,
            "purpose": request.POST.get("purpose", bot.purpose),
            "welcome_message": request.POST.get(
                "welcome_message", bot.welcome_message
            ).strip(),
            "primary_color": request.POST.get(
                "primary_color", bot.primary_color
            ).strip(),
            "position": request.POST.get("position", bot.position),
            "allowed_domains": _parse_domains(request.POST.get("allowed_domains", "")),
            "is_active": "is_active" in request.POST,
        }
        try:
            fields["ai_daily_limit"] = _parse_ai_limit(
                request.POST.get("ai_daily_limit", ""), bot.ai_daily_limit
            )
            if "workflow" in request.POST:
                fields["workflow"] = _parse_workflow(
                    request.POST.get("workflow", ""), workflows
                )
            chatbot_service.update_chatbot(
                bot,
                actor_account=account,
                is_staff=False,
                **fields,
            )
        except (PermissionDenied, ValidationError) as exc:
            detail = "; ".join(getattr(exc, "messages", None) or [str(exc)])
            messages.error(request, detail)
            bot.refresh_from_db()
            return render(
                request,
                "chatbot/edit.html",
                {
                    "bot": bot,
                    "purposes": ChatbotConfig.Purpose.choices,
                    "workflows": workflows,
                    # What they typed, so a mistyped domain can be corrected in place.
                    "domains_display": request.POST.get("allowed_domains", ""),
                    "ai_daily_limit_display": request.POST.get("ai_daily_limit", ""),
                },
            )
        messages.success(request, "Chatbot updated.")
        return redirect("chatbot:detail", pk=bot.pk)

    return render(
        request,
        "chatbot/edit.html",
        {
            "bot": bot,
            "purposes": ChatbotConfig.Purpose.choices,
            "workflows": workflows,
            "domains_display": "\n".join(bot.allowed_domains),
        },
    )


@login_required
@require_POST
def chatbot_allow_site(request: HttpRequest, pk: int) -> HttpResponse:
    """Add a website the widget tried to load on (from the Install check) to the bot's list."""
    from apps.chatbot.services import install

    account = get_current_account(request)
    bot = _bot(request, pk)
    origin = request.POST.get("origin", "")
    if origin not in {b["origin"] for b in install.status(bot)["blocked"]}:
        messages.error(request, "That website isn't waiting to be added any more.")
        return redirect("chatbot:detail", pk=bot.pk)
    chatbot_service.update_chatbot(
        bot,
        actor_account=account,
        allowed_domains=[*bot.allowed_domains, origin],
    )
    messages.success(
        request, f"Added {origin}. Reload that page and the chat button appears."
    )
    return redirect("chatbot:detail", pk=bot.pk)


@login_required
def chatbot_analytics(request: HttpRequest, pk: int) -> HttpResponse:
    bot = _bot(request, pk)

    from apps.chatbot import analytics

    try:
        days = int(request.GET.get("period", 30))
    except (TypeError, ValueError):
        days = 30
    if days not in (7, 30, 90):
        days = 30

    start, end = analytics.period_range(days)
    summary = analytics.session_summary(bot, start, end)
    actions = analytics.action_summary(bot, start, end)
    ticket_count = analytics.tickets_from_chat(bot, start, end)

    return render(
        request,
        "chatbot/analytics.html",
        {
            "bot": bot,
            "days": days,
            "period_options": [
                {"days": 7, "label": "7d"},
                {"days": 30, "label": "30d"},
                {"days": 90, "label": "90d"},
            ],
            "summary": summary,
            "actions": actions,
            "ticket_count": ticket_count,
        },
    )


# ── Helpers ────────────────────────────────────────────────────────────────────


def _setup(bot: ChatbotConfig) -> dict:
    """The "go live" checklist: each step and whether it's done, and whether the bot is live.

    ``conversation_state`` is ``none`` (AI only), ``off`` (never turned on, or paused),
    ``changes`` (on, with saved changes not live yet) or ``on``.
    """
    from apps.automation.models import Workflow
    from apps.chatbot.services import install

    wf = bot.workflow
    if wf is None:
        state = "none"
    elif wf.status != Workflow.Status.PUBLISHED:
        state = "off"
    elif wf.draft_definition is not None:
        state = "changes"
    else:
        state = "on"
    seen = install.status(bot)["live"]
    steps = [
        {
            "key": "conversation",
            "label": "Choose how it talks",
            "done": wf is not None,
        },
        {
            "key": "publish",
            "label": "Turn the conversation on",
            # A bot with no conversation answers with AI alone: nothing to turn on.
            "done": state in ("none", "on", "changes"),
        },
        {
            "key": "website",
            "label": "Add your website",
            "done": bool(bot.allowed_domains),
        },
        {"key": "install", "label": "Paste the code on your site", "done": seen},
    ]
    return {
        "steps": steps,
        "conversation_state": state,
        "done_count": sum(s["done"] for s in steps),
        "live": bot.is_active and all(s["done"] for s in steps),
    }


def _workflow_choices(bot: ChatbotConfig) -> list:
    """Automations this bot can run: the business's, starting from website chat, not archived."""
    from apps.automation.models import Workflow

    return [
        wf
        for wf in Workflow.objects.filter(account=bot.account)
        .exclude(status=Workflow.Status.ARCHIVED)
        .order_by("name")
        if {
            ((wf.definition or {}).get("trigger") or {}).get("type"),
            ((wf.draft_definition or {}).get("trigger") or {}).get("type"),
        }
        & set(WEBSITE_CHAT_TRIGGERS)
    ]


def _parse_workflow(raw: str, choices: list):
    """The chosen automation (None = "AI answers everything")."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for wf in choices:
        if str(wf.pk) == raw:
            return wf
    raise ValidationError(
        "Choose an automation that starts when a visitor opens or messages your website chat."
    )


_MAX_AI_DAILY_LIMIT = 10_000


def _parse_ai_limit(raw: str, current: int) -> int:
    """The owner's "AI answers per day": a whole number from 0 to 10,000 (blank keeps it)."""
    raw = (raw or "").strip()
    if not raw:
        return current
    if not raw.isdigit() or int(raw) > _MAX_AI_DAILY_LIMIT:
        raise ValidationError(
            f"AI answers per day must be a whole number from 0 to {_MAX_AI_DAILY_LIMIT:,}."
        )
    return int(raw)


def _parse_domains(raw: str) -> list[str]:
    return [d.strip().rstrip("/") for d in raw.splitlines() if d.strip()]


def _embed_snippet(bot: ChatbotConfig) -> str:
    from apps.chatbot.embed import snippet

    return snippet(bot.public_key)
