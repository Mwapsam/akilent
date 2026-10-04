"""
Canonical service boundary for creating and updating chatbots.

All ownership rules live here. Views must call these functions rather than
directly calling ChatbotConfig.objects.create() or bot.save(), so the
rules are enforced on every code path including bulk operations.

Ownership invariant:
  SYSTEM  → platform account  → PLATFORM_ALLOWED_ORIGINS only
  CUSTOMER → customer account → customer-controlled domains
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError

if TYPE_CHECKING:
    from apps.accounts.models import Account
    from apps.chatbot.models import ChatbotConfig


def _get_platform_account() -> Account:
    from apps.accounts.models import Account

    account = Account.objects.filter(is_platform_account=True).first()
    if account is None:
        raise RuntimeError("No platform account found. Run migrations to seed it.")
    return account


def _validate_system_domains(domains: list[str]) -> None:
    """Ensure all domains for a system chatbot are Akilent-controlled."""
    allowed_origins: list[str] = getattr(settings, "PLATFORM_ALLOWED_ORIGINS", [])
    site_url = getattr(settings, "SITE_URL", "").rstrip("/")
    if site_url and site_url not in allowed_origins:
        allowed_origins = [*allowed_origins, site_url]

    normalised_allowed = {o.rstrip("/") for o in allowed_origins}
    for domain in domains:
        if domain.rstrip("/") not in normalised_allowed:
            raise ValidationError(
                f"System chatbot domain '{domain}' is not in PLATFORM_ALLOWED_ORIGINS. "
                "System chatbots may only be deployed on Akilent-controlled origins."
            )


def create_customer_chatbot(
    account: Account,
    *,
    name: str,
    purpose: str = "general",
    welcome_message: str = "",
    primary_color: str = "#1a56db",
    position: str = "bottom_right",
    allowed_domains: list[str] | None = None,
    **extra,
) -> ChatbotConfig:
    """Create a customer-owned chatbot. account must not be the platform account."""
    from apps.chatbot.models import ChatbotConfig

    if account.is_platform_account:
        raise ValidationError(
            "Customer chatbots cannot be created on the platform account."
        )
    bot = ChatbotConfig(
        account=account,
        chatbot_type=ChatbotConfig.ChatbotType.CUSTOMER,
        name=name,
        purpose=purpose,
        welcome_message=welcome_message,
        primary_color=primary_color,
        position=position,
        allowed_domains=allowed_domains or [],
        **extra,
    )
    bot.save()
    return bot


def create_system_chatbot(
    *,
    name: str,
    purpose: str = "support",
    welcome_message: str = "",
    primary_color: str = "#1a56db",
    position: str = "bottom_right",
    allowed_domains: list[str] | None = None,
    **extra,
) -> ChatbotConfig:
    """Create a system chatbot owned by the platform account."""
    from apps.chatbot.models import ChatbotConfig

    platform_account = _get_platform_account()
    domains = allowed_domains or []
    _validate_system_domains(domains)

    bot = ChatbotConfig(
        account=platform_account,
        chatbot_type=ChatbotConfig.ChatbotType.SYSTEM,
        name=name,
        purpose=purpose,
        welcome_message=welcome_message,
        primary_color=primary_color,
        position=position,
        allowed_domains=domains,
        **extra,
    )
    bot.save()
    return bot


def update_chatbot(
    bot: ChatbotConfig,
    *,
    actor_account: Account,
    is_staff: bool = False,
    **fields,
) -> ChatbotConfig:
    """Update a chatbot's mutable fields.

    For customer actors: rejects any attempt to change chatbot_type or account
    (raises PermissionDenied — not silently ignored, for security observability).
    For staff actors: chatbot_type and account changes must be passed as explicit
    keyword arguments, not embedded in generic fields dicts.
    """
    from apps.chatbot.models import ChatbotConfig

    # Disallow type/account change via generic fields for non-staff actors.
    if not is_staff:
        if "chatbot_type" in fields:
            raise PermissionDenied("Customers cannot change a chatbot's type.")
        if "account" in fields or "account_id" in fields:
            raise PermissionDenied("Customers cannot change a chatbot's account.")
        # Customer may only edit their own CUSTOMER bots.
        if bot.account_id != actor_account.pk:
            raise PermissionDenied("You do not own this chatbot.")
        if bot.chatbot_type != ChatbotConfig.ChatbotType.CUSTOMER:
            raise PermissionDenied("Customers cannot modify system chatbots.")

    # Strip protected fields from the generic update dict regardless of actor.
    fields.pop("chatbot_type", None)
    fields.pop("account", None)
    fields.pop("account_id", None)
    fields.pop("public_key", None)

    # If staff is changing to system type, validate domains.
    if is_staff and "new_chatbot_type" in fields:
        new_type = fields.pop("new_chatbot_type")
        if new_type == ChatbotConfig.ChatbotType.SYSTEM:
            # Use the incoming domains if provided; fall back to the bot's current
            # domains only after the field dict has been applied (below). Validate
            # here against what will actually be saved, not the old customer domains.
            incoming_domains = fields.get("allowed_domains", [])
            _validate_system_domains(incoming_domains)
            bot.chatbot_type = new_type
            bot.account = _get_platform_account()
        else:
            # Demoting system → customer: must supply a non-platform account.
            new_account = fields.pop("new_account", None)
            if new_account is None:
                raise ValidationError(
                    "A customer account must be supplied when demoting a system chatbot."
                )
            if new_account.is_platform_account:
                raise ValidationError(
                    "Cannot assign a system-to-customer demotion to the platform account."
                )
            bot.chatbot_type = new_type
            bot.account = new_account

    for attr, value in fields.items():
        setattr(bot, attr, value)

    bot.full_clean()
    bot.save()
    return bot
