"""Per-channel post options beyond the caption: alt text, location, collaborators,
Reel settings and a first comment.

Each option applies only to the channel and media types Meta accepts it for
(``CAPABILITIES``); the composer offers only those, and ``parse_options`` drops any
other value, so a provider never receives a field it would reject.

Stored on ``ContentPostVariant.options``:
    Instagram: alt_text, location {id, name}, collaborators [usernames],
               share_to_feed (bool), cover_url, thumb_offset (ms), first_comment
    Facebook:  first_comment
"""

from __future__ import annotations

import json
import re

IMAGE, VIDEO, REEL, STORY, TEXT, CAROUSEL = (
    "image",
    "video",
    "reel",
    "story",
    "text",
    "carousel",
)

# Instagram publishes "video" as a Reel (see providers.instagram), so both get the
# Reel options.
CAPABILITIES: dict[str, dict[str, set[str]]] = {
    "instagram": {
        "alt_text": {IMAGE},  # Meta: image posts only, not Reels, stories or carousels
        "location": {IMAGE, CAROUSEL, VIDEO, REEL},
        "collaborators": {IMAGE, CAROUSEL, VIDEO, REEL},  # not stories
        "share_to_feed": {VIDEO, REEL},
        "cover_url": {VIDEO, REEL},
        "thumb_offset": {VIDEO, REEL},
        "first_comment": {IMAGE, CAROUSEL, VIDEO, REEL},  # stories take no comments
    },
    "facebook": {
        "first_comment": {IMAGE, CAROUSEL, VIDEO, REEL, TEXT},
    },
}

ALT_TEXT_MAX = 1000
COMMENT_MAX = 2200  # Instagram's caption/comment limit; well inside Facebook's
COLLABORATORS_MAX = 3
THUMB_OFFSET_MAX_S = 15 * 60  # Reels run up to 15 minutes
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

# Form field prefix per channel.
_PREFIX = {"instagram": "ig_", "facebook": "fb_"}


class OptionsError(ValueError):
    """An option in the composer form isn't valid."""


def supports(platform: str, option: str, media_type: str) -> bool:
    return media_type in CAPABILITIES.get(platform, {}).get(option, set())


def caps_for_js() -> dict:
    """CAPABILITIES as lists, for the composer's script."""
    return {
        platform: {opt: sorted(types) for opt, types in options.items()}
        for platform, options in CAPABILITIES.items()
    }


def _clean_text(value: str) -> str:
    return (value or "").strip()


def parse_collaborators(raw: str) -> list[str]:
    names: list[str] = []
    for part in re.split(r"[\s,]+", raw or ""):
        name = part.strip().lstrip("@")
        if not name:
            continue
        if not _USERNAME_RE.match(name):
            raise OptionsError(f'"{part}" isn\'t an Instagram username.')
        if name.lower() not in (n.lower() for n in names):
            names.append(name)
    if len(names) > COLLABORATORS_MAX:
        raise OptionsError(
            f"Instagram allows up to {COLLABORATORS_MAX} collaborators on a post."
        )
    return names


def _parse_platform(data, platform: str, media_type: str) -> dict:
    p = _PREFIX[platform]
    out: dict = {}

    if supports(platform, "alt_text", media_type):
        alt = _clean_text(data.get(f"{p}alt_text"))
        if len(alt) > ALT_TEXT_MAX:
            raise OptionsError(f"Keep the alt text under {ALT_TEXT_MAX} characters.")
        if alt:
            out["alt_text"] = alt

    if supports(platform, "location", media_type):
        loc_id = _clean_text(data.get(f"{p}location_id"))
        if loc_id:
            if not loc_id.isdigit():
                raise OptionsError("Pick the location from the search results.")
            name = _clean_text(data.get(f"{p}location_name"))[:200]
            out["location"] = {"id": loc_id, "name": name}

    if supports(platform, "collaborators", media_type):
        names = parse_collaborators(data.get(f"{p}collaborators", ""))
        if names:
            out["collaborators"] = names

    if supports(platform, "share_to_feed", media_type):
        # A checkbox: present means on. The form sends a hidden "0" first, so a form
        # without the field at all (an older form) keeps Instagram's default.
        values = data.getlist(f"{p}share_to_feed") if hasattr(data, "getlist") else []
        if values:
            out["share_to_feed"] = "1" in values

    if supports(platform, "cover_url", media_type):
        cover = _clean_text(data.get(f"{p}cover_url"))
        if cover:
            if not cover.startswith("https://"):
                raise OptionsError("Upload the cover image again.")
            out["cover_url"] = cover

    if supports(platform, "thumb_offset", media_type):
        raw = _clean_text(data.get(f"{p}thumb_offset"))
        if raw:
            try:
                seconds = float(raw)
            except ValueError:
                raise OptionsError(
                    "Enter the cover frame time in seconds, like 3."
                ) from None
            if not 0 <= seconds <= THUMB_OFFSET_MAX_S:
                raise OptionsError(
                    "The cover frame time must be within the video's length."
                )
            if seconds:
                out["thumb_offset"] = int(seconds * 1000)

    if supports(platform, "first_comment", media_type):
        comment = _clean_text(data.get(f"{p}first_comment"))
        if len(comment) > COMMENT_MAX:
            raise OptionsError(
                f"Keep the first comment under {COMMENT_MAX} characters."
            )
        if comment:
            out["first_comment"] = comment

    return out


def parse_options(data, platforms: list[str], media_type: str) -> dict[str, dict]:
    """{platform: options} from the composer form. Raises OptionsError."""
    return {
        platform: _parse_platform(data, platform, media_type)
        for platform in platforms
        if platform in _PREFIX
    }


def instagram_container_params(options: dict, media_type: str) -> dict:
    """The extra /{ig-user}/media parameters for a channel's options.

    Only options valid for ``media_type`` are sent, so options saved for another
    media type (the post was switched from image to Reel) are ignored.
    """
    opts = options or {}
    params: dict = {}

    def ok(option: str) -> bool:
        return option in opts and supports("instagram", option, media_type)

    if ok("alt_text"):
        params["alt_text"] = opts["alt_text"]
    if ok("location") and opts["location"].get("id"):
        params["location_id"] = opts["location"]["id"]
    if ok("collaborators") and opts["collaborators"]:
        params["collaborators"] = json.dumps(opts["collaborators"])
    if ok("share_to_feed"):
        params["share_to_feed"] = "true" if opts["share_to_feed"] else "false"
    if ok("cover_url"):
        params["cover_url"] = opts["cover_url"]
    if ok("thumb_offset"):
        params["thumb_offset"] = opts["thumb_offset"]
    return params


def first_comment_for(variant) -> str:
    opts = variant.options or {}
    if supports(variant.platform, "first_comment", variant.post.media_type):
        return opts.get("first_comment", "")
    return ""
