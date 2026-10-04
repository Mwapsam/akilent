"""Operator console chatbot management — /manage/chatbots/."""

from __future__ import annotations

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.api import Account
from apps.chatbot.api import ChatbotCategory, ChatbotConfig
from apps.chatbot.services import chatbot as chatbot_service
from apps.core.audit import audit
from apps.core.utils import admin_required

# ── Categories ────────────────────────────────────────────────────────────────


@admin_required
def category_list(request):
    return render(
        request,
        "manage/chatbot_categories.html",
        {"categories": ChatbotCategory.objects.prefetch_related("chatbots")},
    )


@admin_required
def category_create(request):
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        description = (request.POST.get("description") or "").strip()
        order = _int_field(request.POST.get("order"), 0)
        is_active = "is_active" in request.POST

        if not name:
            messages.error(request, "Name is required.")
            return render(
                request,
                "manage/chatbot_category_form.html",
                {"post": request.POST, "initial": _cat_initial()},
            )

        slug = slugify(name)
        if ChatbotCategory.objects.filter(slug=slug).exists():
            messages.error(request, f'A category with slug "{slug}" already exists.')
            return render(
                request,
                "manage/chatbot_category_form.html",
                {"post": request.POST, "initial": _cat_initial()},
            )

        try:
            cat = ChatbotCategory.objects.create(
                name=name,
                slug=slug,
                description=description,
                order=order,
                is_active=is_active,
            )
        except IntegrityError:
            messages.error(request, f'A category with slug "{slug}" already exists.')
            return render(
                request,
                "manage/chatbot_category_form.html",
                {"post": request.POST, "initial": _cat_initial()},
            )
        audit(request, "chatbot.category.create", target=cat.name)
        messages.success(request, f'Category "{cat.name}" created.')
        return redirect("core:chatbot-categories")

    return render(
        request, "manage/chatbot_category_form.html", {"initial": _cat_initial()}
    )


@admin_required
def category_edit(request, pk):
    cat = get_object_or_404(ChatbotCategory, pk=pk)

    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        description = (request.POST.get("description") or "").strip()
        order = _int_field(request.POST.get("order"), cat.order)
        is_active = "is_active" in request.POST

        if not name:
            messages.error(request, "Name is required.")
            return render(
                request,
                "manage/chatbot_category_form.html",
                {"category": cat, "post": request.POST, "initial": _cat_initial(cat)},
            )

        cat.name = name
        cat.slug = slugify(name)
        cat.description = description
        cat.order = order
        cat.is_active = is_active
        try:
            cat.save()
        except IntegrityError:
            messages.error(request, f'A category named "{name}" already exists.')
            return render(
                request,
                "manage/chatbot_category_form.html",
                {"category": cat, "post": request.POST, "initial": _cat_initial(cat)},
            )
        audit(request, "chatbot.category.edit", target=cat.name)
        messages.success(request, f'Category "{cat.name}" updated.')
        return redirect("core:chatbot-categories")

    return render(
        request,
        "manage/chatbot_category_form.html",
        {"category": cat, "initial": _cat_initial(cat)},
    )


@admin_required
@require_POST
def category_delete(request, pk):
    cat = get_object_or_404(ChatbotCategory, pk=pk)
    name = cat.name
    # Lock the row so a concurrent chatbot assignment can't slip between the count check and delete.
    with transaction.atomic():
        try:
            cat = ChatbotCategory.objects.select_for_update().get(pk=pk)
        except ChatbotCategory.DoesNotExist:
            # Concurrent delete — treat as success; the record is gone either way.
            messages.success(request, f'Category "{name}" deleted.')
            return redirect("core:chatbot-categories")
        count = cat.chatbots.count()
        if count:
            messages.error(
                request,
                f'Cannot delete "{name}" — {count} chatbot(s) use it. Reassign them first.',
            )
            return redirect("core:chatbot-categories")
        cat.delete()
        audit(request, "chatbot.category.delete", target=name)
    messages.success(request, f'Category "{name}" deleted.')
    return redirect("core:chatbot-categories")


# ── Chatbot cross-account list + edit ────────────────────────────────────────


@admin_required
def chatbot_list(request):
    q = (request.GET.get("q") or "").strip()
    category_pk = request.GET.get("category") or ""
    active_filter = request.GET.get("active") or ""
    type_filter = request.GET.get("chatbot_type") or ""

    bots = ChatbotConfig.objects.select_related("account", "category").order_by(
        "-created_at"
    )
    if q:
        bots = bots.filter(name__icontains=q) | bots.filter(
            account__company_name__icontains=q
        )
        bots = bots.distinct()
    if category_pk.isdigit():
        bots = bots.filter(category_id=int(category_pk))
    if active_filter == "1":
        bots = bots.filter(is_active=True)
    elif active_filter == "0":
        bots = bots.filter(is_active=False)
    if type_filter in ("system", "customer"):
        bots = bots.filter(chatbot_type=type_filter)

    return render(
        request,
        "manage/chatbot_list.html",
        {
            "bots": bots[:200],
            "categories": ChatbotCategory.objects.filter(is_active=True),
            "chatbot_types": ChatbotConfig.ChatbotType.choices,
            "q": q,
            "category_pk": category_pk,
            "active_filter": active_filter,
            "type_filter": type_filter,
        },
    )


@admin_required
def chatbot_create(request):
    from django.core.exceptions import ValidationError

    categories = ChatbotCategory.objects.filter(is_active=True).order_by(
        "order", "name"
    )
    # System chatbots don't need an account selection — they always use the platform account.
    # Customer chatbots need a business account selected.
    accounts = Account.objects.filter(
        is_active=True, is_platform_account=False
    ).order_by("company_name")

    _valid_purposes = {c[0] for c in ChatbotConfig.Purpose.choices}
    _valid_positions = {"bottom_right", "bottom_left"}
    _valid_types = {c[0] for c in ChatbotConfig.ChatbotType.choices}

    if request.method == "POST":
        chatbot_type_raw = (
            request.POST.get("chatbot_type") or ChatbotConfig.ChatbotType.CUSTOMER
        )
        chatbot_type = (
            chatbot_type_raw
            if chatbot_type_raw in _valid_types
            else ChatbotConfig.ChatbotType.CUSTOMER
        )
        account_pk = request.POST.get("account") or ""
        name = (request.POST.get("name") or "").strip()
        _purpose_raw = request.POST.get("purpose") or ChatbotConfig.Purpose.GENERAL
        purpose = (
            _purpose_raw
            if _purpose_raw in _valid_purposes
            else ChatbotConfig.Purpose.GENERAL
        )
        category_pk = request.POST.get("category") or ""
        welcome = (request.POST.get("welcome_message") or "").strip()
        color = (request.POST.get("primary_color") or "#1a56db").strip()
        _position_raw = request.POST.get("position") or "bottom_right"
        position = (
            _position_raw if _position_raw in _valid_positions else "bottom_right"
        )
        is_active = "is_active" in request.POST
        raw_domains = request.POST.get("allowed_domains", "")
        allowed_domains = [
            d.strip().rstrip("/") for d in raw_domains.splitlines() if d.strip()
        ]

        errors = []
        if not name:
            errors.append("Name is required.")
        if chatbot_type == ChatbotConfig.ChatbotType.CUSTOMER and (
            not account_pk or not account_pk.isdigit()
        ):
            errors.append("Business account is required for customer chatbots.")
        if errors:
            for e in errors:
                messages.error(request, e)
            return render(
                request,
                "manage/chatbot_form.html",
                {
                    "accounts": accounts,
                    "categories": categories,
                    "purposes": ChatbotConfig.Purpose.choices,
                    "chatbot_types": ChatbotConfig.ChatbotType.choices,
                    "post": request.POST,
                },
            )

        category = (
            ChatbotCategory.objects.filter(pk=int(category_pk)).first()
            if category_pk.isdigit()
            else None
        )

        try:
            if chatbot_type == ChatbotConfig.ChatbotType.SYSTEM:
                bot = chatbot_service.create_system_chatbot(
                    name=name,
                    purpose=purpose,
                    welcome_message=welcome,
                    primary_color=color,
                    position=position,
                    allowed_domains=allowed_domains,
                    is_active=is_active,
                    category=category,
                )
                audit(request, "chatbot.create.system", target=bot.name)
                messages.success(request, f'System chatbot "{bot.name}" created.')
            else:
                account = get_object_or_404(Account, pk=int(account_pk))
                bot = chatbot_service.create_customer_chatbot(
                    account,
                    name=name,
                    purpose=purpose,
                    welcome_message=welcome,
                    primary_color=color,
                    position=position,
                    allowed_domains=allowed_domains,
                    is_active=is_active,
                    category=category,
                )
                audit(request, "chatbot.create", account, target=bot.name)
                messages.success(
                    request, f'Chatbot "{bot.name}" created for {account.company_name}.'
                )
        except (ValidationError, RuntimeError) as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "manage/chatbot_form.html",
                {
                    "accounts": accounts,
                    "categories": categories,
                    "purposes": ChatbotConfig.Purpose.choices,
                    "chatbot_types": ChatbotConfig.ChatbotType.choices,
                    "post": request.POST,
                },
            )

        return redirect("core:chatbot-list")

    return render(
        request,
        "manage/chatbot_form.html",
        {
            "accounts": accounts,
            "categories": categories,
            "purposes": ChatbotConfig.Purpose.choices,
            "chatbot_types": ChatbotConfig.ChatbotType.choices,
            "initial": _bot_initial(),
        },
    )


@admin_required
def chatbot_edit(request, pk):
    bot = get_object_or_404(
        ChatbotConfig.objects.select_related("account", "category"), pk=pk
    )
    categories = ChatbotCategory.objects.filter(is_active=True).order_by(
        "order", "name"
    )

    _valid_purposes = {c[0] for c in ChatbotConfig.Purpose.choices}
    _valid_positions = {"bottom_right", "bottom_left"}

    if request.method == "POST":
        from django.core.exceptions import ValidationError

        name = (request.POST.get("name") or "").strip()
        if not name:
            messages.error(request, "Name is required.")
            return render(
                request,
                "manage/chatbot_form.html",
                {
                    "bot": bot,
                    "categories": categories,
                    "purposes": ChatbotConfig.Purpose.choices,
                    "chatbot_types": ChatbotConfig.ChatbotType.choices,
                    "post": request.POST,
                    "initial": _bot_initial(bot),
                },
            )
        _purpose_raw = request.POST.get("purpose") or bot.purpose
        purpose = _purpose_raw if _purpose_raw in _valid_purposes else bot.purpose
        category_pk = request.POST.get("category") or ""
        welcome = (request.POST.get("welcome_message") or "").strip()
        color = (request.POST.get("primary_color") or bot.primary_color).strip()
        _position_raw = request.POST.get("position") or bot.position
        position = _position_raw if _position_raw in _valid_positions else bot.position
        is_active = "is_active" in request.POST
        raw_domains = request.POST.get("allowed_domains", "")
        allowed_domains = [
            d.strip().rstrip("/") for d in raw_domains.splitlines() if d.strip()
        ]
        category = (
            (
                ChatbotCategory.objects.filter(pk=int(category_pk)).first()
                if category_pk.isdigit()
                else None
            )
            if "category" in request.POST
            else bot.category
        )

        # Staff may change chatbot_type via new_chatbot_type in the service.
        new_type_raw = request.POST.get("chatbot_type") or ""
        extra = {}
        if new_type_raw and new_type_raw != bot.chatbot_type:
            extra["new_chatbot_type"] = new_type_raw

        try:
            chatbot_service.update_chatbot(
                bot,
                actor_account=bot.account,
                is_staff=True,
                name=name,
                purpose=purpose,
                welcome_message=welcome,
                primary_color=color,
                position=position,
                is_active=is_active,
                allowed_domains=allowed_domains,
                category=category,
                **extra,
            )
        except (ValidationError, RuntimeError) as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "manage/chatbot_form.html",
                {
                    "bot": bot,
                    "categories": categories,
                    "purposes": ChatbotConfig.Purpose.choices,
                    "chatbot_types": ChatbotConfig.ChatbotType.choices,
                    "post": request.POST,
                    "initial": _bot_initial(bot),
                },
            )
        audit(request, "chatbot.edit", bot.account, target=bot.name)
        messages.success(request, f'"{bot.name}" updated.')
        return redirect("core:chatbot-list")

    return render(
        request,
        "manage/chatbot_form.html",
        {
            "bot": bot,
            "categories": categories,
            "purposes": ChatbotConfig.Purpose.choices,
            "initial": _bot_initial(bot),
        },
    )


@admin_required
@require_POST
def chatbot_toggle(request, pk):
    bot = get_object_or_404(ChatbotConfig.objects.select_related("account"), pk=pk)
    bot.is_active = not bot.is_active
    bot.save(update_fields=["is_active", "updated_at"])
    state = "activated" if bot.is_active else "deactivated"
    audit(request, f"chatbot.{state}", bot.account, target=bot.name)
    messages.success(request, f'"{bot.name}" {state}.')
    return redirect("core:chatbot-list")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _bot_initial(bot: ChatbotConfig | None = None) -> dict:
    """Safe defaults for the chatbot form — never references bot attributes directly in template."""
    if bot is None:
        return {
            "name": "",
            "purpose": ChatbotConfig.Purpose.GENERAL,
            "chatbot_type": ChatbotConfig.ChatbotType.CUSTOMER,
            "primary_color": "#1a56db",
            "position": "bottom_right",
            "welcome_message": "",
            "is_active": True,
            "category_id": "",
            "domains_display": "",
        }
    return {
        "name": bot.name,
        "purpose": bot.purpose,
        "chatbot_type": bot.chatbot_type,
        "primary_color": bot.primary_color,
        "position": bot.position,
        "welcome_message": bot.welcome_message,
        "is_active": bot.is_active,
        "category_id": bot.category_id or "",
        "domains_display": "\n".join(bot.allowed_domains),
    }


def _cat_initial(cat: ChatbotCategory | None = None) -> dict:
    """Safe defaults for the category form."""
    if cat is None:
        return {"name": "", "description": "", "order": 0, "is_active": True}
    return {
        "name": cat.name,
        "description": cat.description,
        "order": cat.order,
        "is_active": cat.is_active,
    }


def _int_field(value, default: int) -> int:
    try:
        return max(0, int(value or default))
    except (TypeError, ValueError):
        return default
