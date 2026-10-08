"""Central channel registry.

Each channel is switched on or off by a boolean setting (``WHATSAPP_ENABLED``,
``INSTAGRAM_ENABLED``, ``EMAIL_ENABLED``) whose default lives in settings.py.
Code asks ``is_enabled()`` / ``enabled_channels()`` rather than reading those
settings directly.

A disabled channel keeps its URL module mounted, so ``reverse()`` and links in
messages already sent keep resolving; ``ChannelGateMiddleware`` 404s its pages
instead, apart from ``public_paths`` (callbacks from Meta/SES, tracking and
unsubscribe links) and ``hub_paths`` (pages it shares with another channel).
It also 404s the channel's ``api_paths`` in the public REST API.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChannelDef:
    key: str
    label: str
    setting: str
    url_prefix: str
    # Prefixes (under url_prefix) that stay reachable while the channel is off.
    public_paths: tuple[str, ...] = ()
    # Inbound webhook (under url_prefix). While off, GET still runs the
    # verification handshake and POSTs are acknowledged and dropped, so the
    # provider doesn't disable the subscription.
    webhook_path: str = ""
    # Exact paths (under url_prefix) another channel also uses: path -> that channel.
    hub_paths: dict[str, str] = field(default_factory=dict)
    # Public REST API resources (the part after /api/<version>/) this channel owns.
    api_paths: tuple[str, ...] = ()


CHANNEL_REGISTRY: dict[str, ChannelDef] = {
    "email": ChannelDef(
        key="email",
        label="Email",
        setting="EMAIL_ENABLED",
        url_prefix="email/",
        # SES bounce/complaint notifications, and open/click/unsubscribe links
        # in emails already delivered (unsubscribe must always work).
        public_paths=("webhooks/ses/", "t/"),
        hub_paths={"campaigns/": "whatsapp", "templates/": "whatsapp"},
        api_paths=("messages", "templates", "campaigns", "deliverability"),
    ),
    "whatsapp": ChannelDef(
        key="whatsapp",
        label="WhatsApp",
        setting="WHATSAPP_ENABLED",
        url_prefix="whatsapp/",
        webhook_path="webhook/",
        api_paths=("whatsapp/",),
    ),
    "instagram": ChannelDef(
        key="instagram",
        label="Instagram",
        setting="INSTAGRAM_ENABLED",
        url_prefix="instagram/",
        # Meta's deauthorize and data-deletion callbacks are compliance endpoints.
        public_paths=("deauthorize/", "data-deletion/"),
        webhook_path="webhook/",
    ),
}

# Billing feature keys -> the channel they belong to.
FEATURE_CHANNEL_DEPS: dict[str, str] = {
    "whatsapp": "whatsapp",
    "whatsapp_campaigns": "whatsapp",
    "verification_codes": "whatsapp",
    "instagram": "instagram",
    "email_sending": "email",
    "email_templates": "email",
    "email_campaigns": "email",
    "email_tracking": "email",
}

# Billing limit keys -> the channel they belong to.
LIMIT_CHANNEL_DEPS: dict[str, str] = {
    "conversations_month": "whatsapp",
    "whatsapp_numbers": "whatsapp",
    "whatsapp_marketing_msgs": "whatsapp",
    "whatsapp_utility_msgs": "whatsapp",
    "verification_codes_month": "whatsapp",
    "whatsapp_campaign_recipients": "whatsapp",
    "emails_month": "email",
    "emails_day": "email",
    "email_campaign_recipients": "email",
}


def enabled_channels() -> frozenset[str]:
    """Return the set of channel keys that are currently enabled."""
    from django.conf import settings

    return frozenset(
        ch.key
        for ch in CHANNEL_REGISTRY.values()
        if getattr(settings, ch.setting, False)
    )


def is_enabled(key: str) -> bool:
    return key in enabled_channels()


def feature_visible(key: str, channels: frozenset[str] | set[str]) -> bool:
    dep = FEATURE_CHANNEL_DEPS.get(key)
    return dep is None or dep in channels


def limit_visible(key: str, channels: frozenset[str] | set[str]) -> bool:
    dep = LIMIT_CHANNEL_DEPS.get(key)
    return dep is None or dep in channels
