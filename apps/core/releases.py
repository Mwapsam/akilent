"""Features that are built but not released (docs/marketing/messaging.md section 4).

An unreleased feature keeps its app, models and URL module (so ``reverse()`` and internal
services keep working), but ``ReleaseGateMiddleware`` 404s its pages and templates hide
its entry points via the ``released`` context flag. A feature is released by adding its
key to ``RELEASED_FEATURES`` (env: comma-separated), and then by updating section 4 of
the messaging guide in the same change.
"""

from __future__ import annotations

from django.conf import settings

# key -> URL path prefixes (no leading slash) that belong only to that feature.
UNRELEASED_FEATURES: dict[str, tuple[str, ...]] = {
    "chatbot": ("chatbot/", "api/chat/"),
    "support": ("support/",),
    "hubspot": ("hubspot/",),
}


def is_released(key: str) -> bool:
    return key in getattr(settings, "RELEASED_FEATURES", frozenset())


def released_flags() -> dict[str, bool]:
    return {key: is_released(key) for key in UNRELEASED_FEATURES}


def gated_feature_for_path(path: str) -> str | None:
    """The unreleased feature that owns ``path`` (no leading slash), if any."""
    for key, prefixes in UNRELEASED_FEATURES.items():
        if is_released(key):
            continue
        if any(path.startswith(p) for p in prefixes):
            return key
    return None
