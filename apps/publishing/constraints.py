"""What each channel accepts for a post: caption length, hashtags, media count and kind.

One source for the composer's live counts (sent to the page in ``editor_data``) and
the server's readiness check (``readiness.check``), so the two can't disagree.
"""

from __future__ import annotations

import re

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
