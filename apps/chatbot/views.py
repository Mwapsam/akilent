from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static

from apps.accounts.utils import get_current_account
from apps.chatbot.models import ChatbotAction, ChatbotConfig, ChatbotKnowledgeSource
from apps.chatbot.services import chatbot as chatbot_service
from apps.chatbot.services import knowledge as knowledge_service

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
    from apps.ai.models import KnowledgeBaseEntry

    knowledge_entries = KnowledgeBaseEntry.objects.filter(
        account=account, is_active=True
    )
    linked_ids = set(
        ChatbotKnowledgeSource.objects.filter(chatbot=bot).values_list(
            "knowledge_entry_id", flat=True
        )
    )
    actions = bot.actions.order_by("slug")
    return render(
        request,
        "chatbot/detail.html",
        {
            "bot": bot,
            "knowledge_entries": knowledge_entries,
            "linked_ids": linked_ids,
            "actions": actions,
            "embed_snippet": _embed_snippet(bot),
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
            chatbot_service.update_chatbot(
                bot,
                actor_account=account,
                is_staff=False,
                **fields,
            )
        except (PermissionDenied, ValidationError) as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "chatbot/edit.html",
                {
                    "bot": bot,
                    "purposes": ChatbotConfig.Purpose.choices,
                    "domains_display": "\n".join(bot.allowed_domains),
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
def chatbot_knowledge(request: HttpRequest, pk: int) -> HttpResponse:
    """Toggle a knowledge entry on/off for this chatbot."""
    account = get_current_account(request)
    bot = get_object_or_404(
        ChatbotConfig,
        pk=pk,
        account=account,
        chatbot_type=ChatbotConfig.ChatbotType.CUSTOMER,
    )

    if request.method == "POST":
        from apps.ai.models import KnowledgeBaseEntry

        entry_id = request.POST.get("entry_id", "")
        action = request.POST.get("action", "")
        if entry_id.isdigit():
            entry = get_object_or_404(
                KnowledgeBaseEntry, pk=int(entry_id), account=account
            )
            if action == "add":
                try:
                    knowledge_service.link_knowledge(bot, entry)
                except ValueError as exc:
                    messages.error(request, str(exc))
            elif action == "remove":
                knowledge_service.unlink_knowledge(bot, entry)

    return redirect("chatbot:detail", pk=bot.pk)


@login_required
def chatbot_action_edit(
    request: HttpRequest, pk: int, action_pk: int | None = None
) -> HttpResponse:
    """Create or edit a ChatbotAction for this chatbot."""
    account = get_current_account(request)
    bot = get_object_or_404(
        ChatbotConfig,
        pk=pk,
        account=account,
        chatbot_type=ChatbotConfig.ChatbotType.CUSTOMER,
    )
    action_obj = None
    if action_pk:
        action_obj = get_object_or_404(ChatbotAction, pk=action_pk, chatbot=bot)

    if request.method == "POST":
        slug = request.POST.get("slug", "").strip()
        label = request.POST.get("label", "").strip()
        description = request.POST.get("description", "").strip()
        is_enabled = "is_enabled" in request.POST

        if not slug or not label:
            messages.error(request, "Slug and label are required.")
        elif action_obj:
            action_obj.label = label
            action_obj.description = description
            action_obj.is_enabled = is_enabled
            action_obj.save()
            messages.success(request, "Action updated.")
        else:
            ChatbotAction.objects.create(
                chatbot=bot,
                slug=slug,
                label=label,
                description=description,
                is_enabled=is_enabled,
            )
            messages.success(request, "Action added.")
        return redirect("chatbot:detail", pk=bot.pk)

    return render(
        request,
        "chatbot/action_edit.html",
        {
            "bot": bot,
            "action": action_obj,
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


def _parse_domains(raw: str) -> list[str]:
    return [d.strip().rstrip("/") for d in raw.splitlines() if d.strip()]


def _embed_snippet(bot: ChatbotConfig) -> str:
    widget_url = static("chat-widget/v1/chat.js")
    return f'<script src="{widget_url}"\n        data-chatbot="{bot.public_key}"\n        async></script>'
