"""Central channel registry.

Each channel is switched on or off by a boolean setting (``WHATSAPP_ENABLED``,
``INSTAGRAM_ENABLED``, ``FACEBOOK_ENABLED``, ``EMAIL_ENABLED``) whose default lives in
settings.py. Code asks ``is_enabled()`` / ``enabled_channels()`` rather than reading those
settings directly.

A channel that is on can still be *restricted*: it works for the businesses that can
connect it (e.g. accounts with a role on the Meta app while App Review is pending) but is
never offered to the public. Signup and marketing ask ``public_channels()``.

A disabled channel keeps its URL module mounted, so ``reverse()`` and links in
messages already sent keep resolving; ``ChannelGateMiddleware`` 404s its pages
instead, apart from ``public_paths`` (callbacks from Meta/SES, tracking and
unsubscribe links) and ``hub_paths`` (pages it shares with another channel).
It also 404s the channel's ``api_paths`` in the public REST API.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChannelDef:
    key: str
    label: str
    setting: str
    url_prefix: str
    # Prefixes (under url_prefix) that stay reachable while the channel is off.
    public_paths: tuple[str, ...] = ()
    # Inbound webhook (under url_prefix). GET still runs the verification handshake.
    # A POST on a channel that isn't set up on this server gets 200 and is dropped. A POST
    # on a channel the admin has switched off is validated and stored as held, and gets 200;
    # it is processed when the channel is back on (see apps/core/webhook_replay.py).
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
    # Facebook Page publishing from Content Studio. Facebook messages (Messenger) are not
    # supported, so this channel has no inbox, webhook or API resource.
    "facebook": ChannelDef(
        key="facebook",
        label="Facebook",
        setting="FACEBOOK_ENABLED",
        url_prefix="publishing/fb/",
        # A business can always remove a Page it connected.
        public_paths=("disconnect/",),
    ),
}

# Channels restricted until Meta approves the app's permissions (see restricted_channels).
META_REVIEW_CHANNELS = ("facebook", "instagram")

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

# Billing feature keys that need at least one of several channels.
FEATURE_ANY_CHANNEL_DEPS: dict[str, tuple[str, ...]] = {
    "content_studio": ("instagram", "facebook"),
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


class ChannelError(Exception):
    """Base for "this send can't go out on its channel right now"."""


def channel_error_status(exc: ChannelError) -> int:
    """HTTP status for a channel error: 503 while settings load (retry), else 403."""
    return 503 if isinstance(exc, ChannelStateUnknownError) else 403


class ChannelDisabledError(ChannelError):
    """Raised before an operation on a channel the platform admin has switched off."""

    def __init__(self, key: str, message: str | None = None):
        self.key = key
        super().__init__(message or f"{CHANNEL_REGISTRY[key].label} is disabled.")


class ChannelUnavailableError(Exception):
    """Raised when an admin tries to enable a channel this server is not set up for."""


# The admin's disabled list, cached per process. ``value`` is the last list read from the
# database and survives failed reloads, so a database outage keeps the last decision.
_CACHE_TTL_SECONDS = 5.0
_RETRY_AFTER_FAILURE_SECONDS = 10.0
_cache_lock = threading.Lock()
_cache: dict = {"value": None, "loaded_at": 0.0, "failed_at": 0.0, "generation": 0}
logger = logging.getLogger(__name__)


def available_channels() -> frozenset[str]:
    """Channels this server is set up for (env flags). The admin cannot enable more than this."""
    from django.conf import settings

    return frozenset(
        ch.key
        for ch in CHANNEL_REGISTRY.values()
        if getattr(settings, ch.setting, False)
    )


class ChannelStateUnknownError(ChannelError):
    """The admin's channel settings could not be read, so the state is not known yet.

    Sends are retried later rather than dropped or refused permanently.
    """

    def __init__(self, key: str):
        self.key = key
        label = CHANNEL_REGISTRY[key].label if key in CHANNEL_REGISTRY else key
        super().__init__(f"{label} is temporarily unavailable, try again shortly.")


def _read_disabled_from_db() -> frozenset[str] | None:
    from apps.core.models import SiteSettings

    try:
        raw = SiteSettings.load().disabled_channels or []
    except Exception:
        logger.exception("Channel settings could not be read")
        return None
    return frozenset(k for k in raw if k in CHANNEL_REGISTRY)


def disabled_channels() -> frozenset[str] | None:
    """Channels the admin has switched off, or ``None`` when that is not known yet.

    A failed reload keeps the last good value. ``None`` only happens before the first
    successful read in this process.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["value"]
        if cached is not None and now - _cache["loaded_at"] < _CACHE_TTL_SECONDS:
            return cached
        if now - _cache["failed_at"] < _RETRY_AFTER_FAILURE_SECONDS:
            return cached  # the database failed moments ago; don't hammer it
        generation = _cache["generation"]
    loaded = _read_disabled_from_db()
    if loaded is None:
        with _cache_lock:
            _cache["failed_at"] = time.monotonic()
            return _cache["value"]
    with _cache_lock:
        # Store only if no admin change was saved while this read was in flight.
        if _cache["generation"] == generation:
            _cache.update(value=loaded, loaded_at=now)
    return loaded


def invalidate_channel_cache() -> None:
    """Force the next read to hit the database (last good value is kept for outages)."""
    with _cache_lock:
        _cache["loaded_at"] = 0.0
        _cache["generation"] += 1


def enabled_channels() -> frozenset[str]:
    """Return the set of channel keys that are currently enabled.

    Enabled = available on this server AND not switched off by the admin. When the admin
    settings are unknown, nothing is shown as enabled (pages hide, nothing new is offered).
    """
    disabled = disabled_channels()
    if disabled is None:
        return frozenset()
    return available_channels() - disabled


def channel_state(key: str) -> str:
    """ "enabled", "disabled" (server not set up, or admin switched off), or "unknown"."""
    if key not in available_channels():
        return "disabled"
    disabled = disabled_channels()
    if disabled is None:
        return "unknown"
    return "disabled" if key in disabled else "enabled"


def is_enabled(key: str) -> bool:
    return key in enabled_channels()


def hide_disabled_channels(qs, field: str = "channel"):
    """Exclude rows whose channel is switched off (existing data stays in the database)."""
    hidden = sorted(set(CHANNEL_REGISTRY) - enabled_channels())
    return qs.exclude(**{f"{field}__in": hidden}) if hidden else qs


def visible_choices(choices):
    """Filter ``(value, label)`` pairs down to the ones whose channel is enabled.

    Values that are not registry channels (e.g. SMS) are kept as they are.
    """
    enabled = enabled_channels()
    return [(v, l) for v, l in choices if v not in CHANNEL_REGISTRY or v in enabled]


def assert_channel_enabled(key: str) -> None:
    """Send-boundary check.

    Raises ``ChannelDisabledError`` when the channel is off (permanent for this send), and
    ``ChannelStateUnknownError`` when the settings can't be read yet (retry later).
    """
    state = channel_state(key)
    if state == "disabled":
        raise ChannelDisabledError(key)
    if state == "unknown":
        raise ChannelStateUnknownError(key)


def set_channel_enabled(key: str, enabled: bool, *, request) -> bool:
    """Switch a channel on or off. Returns True if the state changed.

    Locks the settings row so concurrent admin changes to different channels both persist,
    and writes the audit entry in the same transaction.
    """
    from django.db import transaction

    from apps.core.audit import audit
    from apps.core.models import SiteSettings

    if key not in CHANNEL_REGISTRY:
        raise ValueError(f"Unknown channel: {key}")
    if enabled and key not in available_channels():
        raise ChannelUnavailableError(key)

    SiteSettings.load()  # make sure the singleton row exists before locking it
    with transaction.atomic():
        site = SiteSettings.objects.select_for_update().get(pk=1)
        current = set(site.disabled_channels or [])
        was_enabled = key not in current
        if was_enabled == enabled:
            return False
        if enabled:
            current.discard(key)
        else:
            current.add(key)
        site.disabled_channels = sorted(current)
        site.save(update_fields=["disabled_channels", "updated_at"])
        audit(
            request,
            "channel.enabled" if enabled else "channel.disabled",
            target=key,
            channel=key,
            previous=was_enabled,
            new=enabled,
        )
    invalidate_channel_cache()
    if enabled:
        _kick_webhook_replay()
    return True


def _kick_webhook_replay() -> None:
    """Release webhooks held while the channel was off. The periodic sweep is the backstop."""
    try:
        from apps.core.webhook_replay import replay_held_webhooks

        replay_held_webhooks.delay()
    except Exception:
        logger.exception(
            "could not start webhook replay; the periodic sweep will retry"
        )


def restricted_channels() -> frozenset[str]:
    """Channels that are on but not offered to the public (signup, marketing, plan cards).

    If the setting can't be read, every channel counts as restricted: offering a channel
    by mistake is worse than briefly hiding one.
    """
    from apps.core.models import SiteSettings

    try:
        raw = SiteSettings.load().restricted_channels or []
    except Exception:
        logger.exception("Channel restrictions could not be read")
        return frozenset(CHANNEL_REGISTRY)
    return frozenset(k for k in raw if k in CHANNEL_REGISTRY)


def public_channels() -> frozenset[str]:
    """Channels a new customer may be offered: enabled and not restricted."""
    return enabled_channels() - restricted_channels()


def set_channel_public(key: str, public: bool, *, request) -> bool:
    """Open a channel to the public or restrict it. Returns True if the state changed."""
    from django.db import transaction

    from apps.core.audit import audit
    from apps.core.models import SiteSettings

    if key not in CHANNEL_REGISTRY:
        raise ValueError(f"Unknown channel: {key}")
    SiteSettings.load()
    with transaction.atomic():
        site = SiteSettings.objects.select_for_update().get(pk=1)
        current = set(site.restricted_channels or [])
        was_public = key not in current
        if was_public == public:
            return False
        if public:
            current.discard(key)
        else:
            current.add(key)
        site.restricted_channels = sorted(current)
        site.save(update_fields=["restricted_channels", "updated_at"])
        audit(
            request,
            "channel.opened" if public else "channel.restricted",
            target=key,
            channel=key,
            previous=was_public,
            new=public,
        )
    return True


def feature_visible(key: str, channels: frozenset[str] | set[str]) -> bool:
    dep = FEATURE_CHANNEL_DEPS.get(key)
    any_of = FEATURE_ANY_CHANNEL_DEPS.get(key)
    if any_of is not None and not set(any_of) & set(channels):
        return False
    return dep is None or dep in channels


def limit_visible(key: str, channels: frozenset[str] | set[str]) -> bool:
    dep = LIMIT_CHANNEL_DEPS.get(key)
    return dep is None or dep in channels
