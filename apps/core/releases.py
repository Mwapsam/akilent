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
    # Facebook stories and carousels on their own endpoints. No pages of its own:
    # until released, apps/publishing/constraints.py blocks those formats on Facebook.
    "cs_fb_formats": (),
    # Link-in-bio page (apps/publishing/bio.py): the public page and its management.
    "cs_bio_page": ("b/", "publishing/bio/"),
    # "Which posts sold" report (apps/publishing/sales.py).
    "cs_sales_report": ("publishing/sales/",),
    # Month calendar with drag to reschedule, posting slots and best times
    # (apps/publishing/scheduling.py). The month view itself checks the flag.
    "cs_scheduling": (
        "publishing/calendar/move/",
        "publishing/calendar/move-plan/",
        "publishing/slots/",
    ),
    # Account analytics, CSV/print reports and the weekly email
    # (apps/publishing/account_metrics.py).
    "cs_analytics": ("publishing/analytics/",),
    # Recurring posts (apps/publishing/recurring.py).
    "cs_recurring": ("publishing/recurring/",),
    # Bulk import from CSV, drafts only (apps/publishing/csv_import.py).
    "cs_csv_import": ("publishing/import/",),
    # Media library, picture editor, Reel covers, stock photos, caption templates and
    # hashtag sets (apps/publishing/media_library.py).
    "cs_media_library": ("publishing/media/",),
    # Content Studio AI tools: brand voice, rewrite in place, hashtag suggestions and post
    # ideas from results (apps/publishing/ai_plus.py). The composer tools check the flag.
    "cs_ai_plus": ("publishing/ai/",),
    # Direct-to-S3 uploads (apps/publishing/direct_upload.py) and Meta's byte upload as
    # the fallback when Facebook can't fetch a video by URL. Also needs cs_fb_formats.
    "cs_direct_upload": ("publishing/posts/media/direct/",),
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
