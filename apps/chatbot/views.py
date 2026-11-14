from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.utils import get_current_account
from apps.chatbot.models import ChatbotConfig
from apps.chatbot.services import chatbot as chatbot_service

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse

logger = logging.getLogger(__name__)


@login_required
def chatbot_list(request: HttpRequest) -> HttpResponse:
    account = get_current_account(request)
    bots = ChatbotConfig.objects.filter(
        account=account, chatbot_type=ChatbotConfig.ChatbotType.CUSTOMER
    ).order_by("-created_at")
    return render(request, "chatbot/list.html", {"bots": bots})


@login_required
def chatbot_create(request: HttpRequest) -> HttpResponse:
    account = get_current_account(request)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        purpose = request.POST.get("purpose", ChatbotConfig.Purpose.GENERAL)
        welcome = request.POST.get("welcome_message", "").strip()
        color = request.POST.get("primary_color", "#1a56db").strip()
        position = request.POST.get("position", "bottom_right")

        if not name:
            return render(
                request,
                "chatbot/create.html",
                {
                    "error": "Name is required.",
                    "post": request.POST,
                    "purposes": ChatbotConfig.Purpose.choices,
                },
            )

        # chatbot_type and account are set server-side; submitted values are ignored.
        bot = chatbot_service.create_customer_chatbot(
            account,
            name=name,
            purpose=purpose,
            welcome_message=welcome,
            primary_color=color,
            position=position,
        )
        messages.success(request, f'Chatbot "{bot.name}" created.')
        return redirect("chatbot:detail", pk=bot.pk)

    return render(
        request,
        "chatbot/create.html",
        {
            "purposes": ChatbotConfig.Purpose.choices,
        },
    )


@login_required
def chatbot_detail(request: HttpRequest, pk: int) -> HttpResponse:
    account = get_current_account(request)
    bot = get_object_or_404(
        ChatbotConfig,
        pk=pk,
        account=account,
        chatbot_type=ChatbotConfig.ChatbotType.CUSTOMER,
    )
    from django.conf import settings

    from apps.ai import api as ai_api
    from apps.chatbot.services.ai_budget import used_today

    return render(
        request,
        "chatbot/detail.html",
        {
            "bot": bot,
            "embed_snippet": _embed_snippet(bot),
            # How it answers: AI from the business's Knowledge, within the bot's daily limit.
            "ai_unavailable": ai_api.unavailable_reason(account),
            "ai_autonomy": getattr(settings, "AI_AUTONOMY_ENABLED", True),
            "ai_used_today": min(used_today(bot.pk), bot.ai_daily_limit),
        },
    )


@login_required
def chatbot_edit(request: HttpRequest, pk: int) -> HttpResponse:
    account = get_current_account(request)
    bot = get_object_or_404(
        ChatbotConfig,
        pk=pk,
        account=account,
        chatbot_type=ChatbotConfig.ChatbotType.CUSTOMER,
    )

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
            "domains_display": "\n".join(bot.allowed_domains),
        },
    )


@login_required
def chatbot_analytics(request: HttpRequest, pk: int) -> HttpResponse:
    account = get_current_account(request)
    bot = get_object_or_404(
        ChatbotConfig,
        pk=pk,
        account=account,
        chatbot_type=ChatbotConfig.ChatbotType.CUSTOMER,
    )

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
