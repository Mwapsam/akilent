"""Content Studio AI tools: rewrite a caption, suggest hashtags, and post ideas from results.

The model only proposes words. Each answer is checked here, shown to the person, and used
only when they pick it; the post then goes through the composer's normal save, readiness
and approval like anything typed by hand (``apps.publishing.ai_plus`` builds the context
and records what was used). Nothing here creates a post.
"""

from __future__ import annotations

import re

from apps.ai import prompts
from apps.ai.drafting import DraftError, _business_block, word_diff
from apps.ai.proposals import extract_json
from apps.ai.types import ChatMessage

REWRITES = {
    "shorter": "Make it shorter and easier to read, keeping every fact.",
    "friendlier": "Make it warmer and friendlier, same meaning, similar length.",
    "direct": "Make the next step clearer and more direct, without inventing any offer.",
    "fix": "Fix spelling, grammar and punctuation only. Change nothing else.",
    "instagram": "Adapt it for Instagram: a strong first line and short paragraphs.",
    "facebook": "Adapt it for a Facebook Page: conversational and easy to read.",
}
MAX_IDEAS = 5
MAX_TAG = 60
MAX_TOPIC = 200
MAX_ANGLE = 600

_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"(?<![\w@])@[\w.]+\w")
_HASHTAG = re.compile(r"(?<![\w#])#\w+")
_TAG_OK = re.compile(r"^\w+$")


def _voice_block(voice: str) -> str:
    voice = (voice or "").strip()
    if not voice:
        return ""
    return (
        "\n\nHow this business writes (follow the style; it is not a source of facts):\n"
        + voice[:1500]
    )


def _keepers(text: str) -> list[str]:
    """Links, phone numbers, @mentions and hashtags a rewrite must keep, in order."""
    found = [m.rstrip(".,!?;:)") for m in _URL.findall(text)]
    found += [m.group(0) for m in prompts._PHONE.finditer(text)]
    found += _MENTION.findall(text) + _HASHTAG.findall(text)
    return list(dict.fromkeys(found))


def _new_facts(account, before: str, after: str) -> list[str]:
    from apps.ai import api as ai_api

    had = set(ai_api.unchecked_facts(account, before))
    return [f for f in ai_api.unchecked_facts(account, after) if f not in had]


def _ask(provider, system: str, user: str) -> tuple[dict, str]:
    result = provider.chat(
        [ChatMessage("user", user)], system=system, max_tokens=1500, temperature=0.4
    )
    return extract_json(result.text), (result.model or "")[:80]


# ---- rewrite ------------------------------------------------------------------------------
def rewrite_prompt(account, notes: str, instruction: str, voice: str) -> str:
    return f"""You edit social media captions for a small business.

{REWRITES[instruction]}
Keep every link, phone number, @mention and hashtag exactly as written. Never add prices, \
dates, discounts, offers or other facts that aren't in the caption or the notes below.

Answer with ONE JSON object and nothing else: {{"caption": "<the new caption>"}}

About the business:
{_business_block(account, notes)}{_voice_block(voice)}"""


def check_rewrite(account, before: str, data: dict, limit=None) -> tuple[dict, list]:
    after = str(data.get("caption") or "").strip()
    if not after:
        raise DraftError("The rewrite came back empty. Try again.")
    if after == before:
        raise DraftError("The rewrite came back unchanged. Try another option.")
    warnings = []
    dropped = [k for k in _keepers(before) if k not in after]
    if dropped:
        warnings.append(
            "It left out " + ", ".join(dropped[:3]) + ". Add it back if you need it."
        )
    if limit and len(after) > int(limit):
        warnings.append(
            f"It's {len(after):,} characters, over the {int(limit):,} allowed here."
        )
    new = _new_facts(account, before, after)
    if new:
        warnings.append(
            "Check "
            + ", ".join(new[:3])
            + ": it wasn't in your caption or your notes, so make sure it's right."
        )
    result = {
        "before": before,
        "after": after,
        "diff": word_diff(before, after),
        # The same shape as caption suggestions, so the post can record what it used.
        "options": [{"caption": after, "hashtags": []}],
    }
    return result, warnings


def _run_rewrite(draft, provider, notes: str) -> None:
    ctx = draft.context
    before = str(ctx.get("caption") or "").strip()
    instruction = ctx.get("instruction", "")
    if not before or instruction not in REWRITES:
        raise DraftError("Nothing to rewrite.")
    system = rewrite_prompt(draft.account, notes, instruction, ctx.get("voice", ""))
    data, draft.model = _ask(provider, system, before)
    draft.result, draft.warnings = check_rewrite(
        draft.account, before, data, ctx.get("limit")
    )


# ---- hashtags -----------------------------------------------------------------------------
def hashtags_prompt(platform: str, room: int, voice: str) -> str:
    return f"""You suggest hashtags for a small business's {platform} post.

Suggest up to {room} hashtags that fit the caption: a mix of specific and widely used ones \
people actually search. No spaces, no "#", nothing misleading or unrelated to the post.

Answer with ONE JSON object and nothing else: {{"hashtags": ["<tag>", ...]}}{_voice_block(voice)}"""


def check_hashtags(data: dict, existing: list[str], room: int) -> list[str]:
    taken = {t.lower() for t in existing}
    out: list[str] = []
    for raw in data.get("hashtags") or []:
        tag = str(raw).strip().lstrip("#").strip()
        if not tag or len(tag) > MAX_TAG or not _TAG_OK.match(tag):
            continue
        if tag.lower() in taken:
            continue
        taken.add(tag.lower())
        out.append(tag)
    out = out[: max(room, 0)]
    if not out:
        raise DraftError("No new hashtags came back. Try again.")
    return out


def _run_hashtags(draft, provider) -> None:
    ctx = draft.context
    caption = str(ctx.get("caption") or "").strip()
    room = int(ctx.get("room") or 0)
    if not caption or room <= 0:
        raise DraftError("Nothing to suggest hashtags for.")
    existing = [str(t) for t in ctx.get("existing") or []]
    user = caption
    if existing:
        user += "\n\nAlready used (don't repeat): " + " ".join(existing)
    system = hashtags_prompt(
        ctx.get("platform", "instagram"), room, ctx.get("voice", "")
    )
    data, draft.model = _ask(provider, system, user)
    draft.result = {"hashtags": check_hashtags(data, existing, room)}
    draft.warnings = []


# ---- ideas from results -------------------------------------------------------------------
def ideas_prompt(account, notes: str, media_types: list[str], voice: str) -> str:
    return f"""You plan social media posts for a small business.

Below are its posts that started the most WhatsApp chats, leads and paid orders, with \
the numbers. Suggest up to {MAX_IDEAS} new post ideas that build on what worked. Each idea \
names the post it builds on by id. Never invent facts, prices or offers that aren't in the \
posts or the notes below.

Media types allowed: {", ".join(media_types)}.

Answer with ONE JSON object and nothing else:
{{"ideas": [{{"topic": "<a short title>", "angle": "<what to say and why it should work>", \
"media_type": "<one of the allowed types>", "based_on": <post id>}}]}}

About the business:
{_business_block(account, notes)}{_voice_block(voice)}"""


def check_ideas(account, data: dict, sources: list[dict], media_types: list[str]):
    ids = {int(s["id"]) for s in sources}
    ideas, warnings = [], []
    for raw in data.get("ideas") or []:
        if not isinstance(raw, dict):
            continue
        topic = " ".join(str(raw.get("topic") or "").split())[:MAX_TOPIC]
        angle = str(raw.get("angle") or "").strip()[:MAX_ANGLE]
        try:
            based_on = int(raw.get("based_on") or "")
        except (TypeError, ValueError):
            continue
        # An idea that can't name one of the posts it was given isn't "from results".
        if not topic or based_on not in ids:
            continue
        media_type = raw.get("media_type")
        ideas.append(
            {
                "topic": topic,
                "angle": angle,
                "media_type": media_type if media_type in media_types else "image",
                "based_on": based_on,
            }
        )
        if len(ideas) == MAX_IDEAS:
            break
    if not ideas:
        raise DraftError("No usable ideas came back. Try again.")
    source_text = "\n".join(str(s.get("caption") or "") for s in sources)
    new = _new_facts(
        account, source_text, "\n".join(f"{i['topic']} {i['angle']}" for i in ideas)
    )
    if new:
        warnings.append(
            "Check "
            + ", ".join(new[:3])
            + ": it isn't in your posts or your notes, so make sure it's right."
        )
    return {"ideas": ideas}, warnings


def _run_ideas(draft, provider, notes: str) -> None:
    ctx = draft.context
    sources = ctx.get("sources") or []
    media_types = ctx.get("media_types") or ["image"]
    if not sources:
        raise DraftError("There aren't enough results to learn from yet.")
    lines = []
    for s in sources:
        lines.append(
            f"Post {s['id']} ({s.get('media_type', '')} on {', '.join(s.get('platforms') or [])}): "
            f"{s.get('chats', 0)} chats, {s.get('leads', 0)} leads, "
            f"{s.get('paid_orders', 0)} paid orders.\nCaption: {s.get('caption', '')}"
        )
    system = ideas_prompt(draft.account, notes, media_types, ctx.get("voice", ""))
    data, draft.model = _ask(provider, system, "\n\n".join(lines))
    draft.result, draft.warnings = check_ideas(
        draft.account, data, sources, media_types
    )


def run(draft, provider, notes: str) -> None:
    """Fill a content_rewrite / content_hashtags / content_ideas draft."""
    if draft.kind == "content_rewrite":
        _run_rewrite(draft, provider, notes)
    elif draft.kind == "content_hashtags":
        _run_hashtags(draft, provider)
    elif draft.kind == "content_ideas":
        _run_ideas(draft, provider, notes)
    else:
        raise DraftError("Unknown content tool.")
