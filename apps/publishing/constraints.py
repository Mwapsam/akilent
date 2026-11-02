"""What each channel accepts for a post: caption length, hashtags, media count and kind.

One source for the composer's live counts (sent to the page in ``editor_data``) and
the server's readiness check (``readiness.check``), so the two can't disagree.
"""

from __future__ import annotations

import re
from typing import Any

IMAGE, VIDEO, REEL, STORY, TEXT, CAROUSEL = (
    "image",
    "video",
    "reel",
    "story",
    "text",
    "carousel",
)

CAPTION_MAX = {"instagram": 2200, "facebook": 63206}
HASHTAGS_MAX = {"instagram": 30}
CAROUSEL_MIN = 2
CAROUSEL_MAX = 10
# Share of the limit at which the composer's count turns amber.
NEAR_LIMIT = 0.9

# Instagram feed images: portrait 4:5 up to landscape 1.91:1 (a 1% tolerance).
IG_RATIO_MIN = 0.8 * 0.99
IG_RATIO_MAX = 1.91 * 1.01

# Media types each channel can publish. Facebook publishes a story as a photo or video
# post, and Instagram has no text-only post.
MEDIA_TYPES = {
    "instagram": {IMAGE, VIDEO, REEL, STORY, CAROUSEL},
    "facebook": {IMAGE, VIDEO, REEL, STORY, TEXT, CAROUSEL},
}

# Facebook formats we can't publish correctly yet: a story would go out as a regular
# post and a carousel as its first image only. Readiness blocks them and dispatch fails
# them, until the ``cs_fb_formats`` release adds Facebook's own story and multi-photo
# endpoints.
FB_FORMAT_FLAG = "cs_fb_formats"
FB_UNSUPPORTED = {STORY, CAROUSEL}
FB_UNSUPPORTED_MESSAGE = (
    "Not yet supported on Facebook. Post to Instagram, or use an image post."
)
FB_REEL_AS_VIDEO_MESSAGE = "Publishes as a regular video on Facebook for now."


def fb_formats_released() -> bool:
    """Facebook Reels, stories and multi-photo posts on their own endpoints, and the
    server-side media checks (Phase 0a)."""
    from apps.core.releases import is_released

    return is_released(FB_FORMAT_FLAG)


def facebook_format_blocked(media_type: str) -> bool:
    """Whether Facebook must refuse ``media_type`` because it would publish wrongly."""
    return media_type in FB_UNSUPPORTED and not fb_formats_released()


# Video limits per channel and format, from Meta's docs for Graph API v25 (checked
# 2026-11-01 with the Meta developer docs MCP): Facebook "Publish a Reel" and "Stories",
# Instagram "IG User Media" (Reel and Story Video Specifications). Instagram publishes
# every video as a Reel, so its feed video takes the Reel limits. A bound of None isn't
# checked. ``ratio`` is (min, max) width/height.
VIDEO_LIMITS: dict[tuple[str, str], dict[str, Any]] = {
    ("facebook", REEL): {
        "duration": (3, 90),
        "fps": (24, 60),
        "min_size": (540, 960),
        "ratio": (9 / 16 * 0.98, 9 / 16 * 1.02),
    },
    ("facebook", STORY): {
        "duration": (3, 60),
        "fps": (24, 60),
        "min_size": (540, 960),
        "ratio": (9 / 16 * 0.98, 9 / 16 * 1.02),
    },
    ("instagram", REEL): {
        "duration": (3, 15 * 60),
        "fps": (23, 60),
        "max_bytes": 300 * 1024 * 1024,
        "ratio": (0.01, 10),
    },
    ("instagram", VIDEO): {
        "duration": (3, 15 * 60),
        "fps": (23, 60),
        "max_bytes": 300 * 1024 * 1024,
        "ratio": (0.01, 10),
    },
    ("instagram", STORY): {
        "duration": (3, 60),
        "fps": (23, 60),
        "max_bytes": 100 * 1024 * 1024,
        "ratio": (0.1, 10),
    },
}

# Facebook allows 30 API-published Reels per Page in any rolling 24 hours. Our own count
# only sees Reels sent from Akilent, so it is an early warning; Meta's error decides.
FB_REELS_PER_DAY = 30


# The kind of file each media type takes.
MEDIA_KIND = {
    IMAGE: {"image"},
    CAROUSEL: {"image"},
    VIDEO: {"video"},
    REEL: {"video"},
    STORY: {"image", "video"},
    TEXT: set(),
}

_IMAGE_RE = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.IGNORECASE)
_VIDEO_RE = re.compile(r"\.(mp4|mov|m4v|webm|avi)(\?|$)", re.IGNORECASE)


def media_kind(url: str) -> str:
    """The file's kind: "image", "video", or "" when the URL doesn't tell."""
    if _IMAGE_RE.search(url or ""):
        return "image"
    if _VIDEO_RE.search(url or ""):
        return "video"
    return ""


def for_js() -> dict:
    """The limits the composer's script needs."""
    return {
        "caption_max": CAPTION_MAX,
        "hashtags_max": HASHTAGS_MAX,
        "carousel_min": CAROUSEL_MIN,
        "carousel_max": CAROUSEL_MAX,
        "near_limit": NEAR_LIMIT,
        "media_types": {p: sorted(t) for p, t in MEDIA_TYPES.items()},
        "media_kind": {t: sorted(k) for t, k in MEDIA_KIND.items()},
    }
