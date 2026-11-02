"""Content Studio AI tools (``cs_ai_plus``): brand voice, rewrite in place, hashtag
suggestions, and post ideas from results.

AI stays optional and never acts on its own:

* every tool needs the flag released and AI on for the business (``apps.ai.api.is_available``:
  the site's provider, the plan, and the owner's opt-in);
* the model's answer is a suggestion the person picks or ignores. Picked text goes through the
  composer's normal save, readiness and approval like anything typed, and the post is marked
  AI-assisted (``record_assist``);
* ideas become plan items (Planning tab) only, never posts.

The model calls and their checks live in ``apps.ai.content``.
"""

from __future__ import annotations

from apps.ai import api as ai_api
from apps.core.releases import is_released

from . import constraints

FLAG = "cs_ai_plus"
MAX_VOICE = 1500
MAX_CAPTION = 5000
IDEA_DAYS = 90
IDEA_SOURCES = 5
MAX_ASSIST = 20
# How many hashtags to suggest at most per channel; Instagram's hard limit still applies.
SUGGEST_TAGS = {"instagram": 10, "facebook": 3}

REWRITE_LABELS = {
    "shorter": "Shorter",
    "friendlier": "Friendlier",
    "direct": "Clearer next step",
    "fix": "Fix spelling and grammar",
    "instagram": "Adapt for Instagram",
    "facebook": "Adapt for Facebook",
}


class AiPlusError(Exception):
    """The request can't be made; the message is for the person."""


def available(account) -> bool:
    return is_released(FLAG) and ai_api.is_available(account)


def off_reason(account) -> str | None:
    """Why the tools are off for this business, in plain words, or None."""
    if not is_released(FLAG):
        return "Not available yet."
    return ai_api.unavailable_reason(account)


def voice(account) -> str:
    """The brand voice to pass to the model; empty until released."""
    if not is_released(FLAG):
        return ""
    return ai_api.settings_for(account).brand_voice or ""


def save_voice(account, text: str) -> None:
    from apps.ai.models import AISettings

    ai_settings, _ = AISettings.objects.get_or_create(account=account)
    ai_settings.brand_voice = (text or "").strip()[:MAX_VOICE]
    ai_settings.save(update_fields=["brand_voice", "updated_at"])


def _request(account, user, kind: str, prompt: str, context: dict):
    if not available(account):
        raise AiPlusError(off_reason(account) or "AI is off.")
    draft = ai_api.request_draft(
        account, user, kind, prompt, {**context, "voice": voice(account)}
    )
    if draft is None:
        raise AiPlusError(off_reason(account) or "AI is off.")
    return draft


def request_rewrite(account, user, caption: str, instruction: str, platform: str):
    caption = (caption or "").strip()
    if instruction not in REWRITE_LABELS:
        raise AiPlusError("Choose how to rewrite it.")
    if not caption:
        raise AiPlusError("Write a caption first.")
    if len(caption) > MAX_CAPTION:
        raise AiPlusError("That caption is too long to rewrite.")
    return _request(
        account,
        user,
        "content_rewrite",
        REWRITE_LABELS[instruction],
        {
            "caption": caption,
            "instruction": instruction,
            "platform": platform,
            "limit": constraints.CAPTION_MAX.get(platform),
        },
    )


def hashtag_room(platform: str, existing: list[str]) -> int:
    want = SUGGEST_TAGS.get(platform, 5)
    cap = constraints.HASHTAGS_MAX.get(platform)
    if cap is not None:
        want = min(want, cap - len(existing))
    return max(want, 0)


def request_hashtags(account, user, caption: str, existing: list[str], platform: str):
    caption = (caption or "").strip()
    if not caption:
        raise AiPlusError("Write a caption first, so the hashtags fit it.")
    existing = [t.lstrip("#") for t in existing if t.strip("#")][:60]
    room = hashtag_room(platform, existing)
    if room <= 0:
        raise AiPlusError("This post already has as many hashtags as it can take.")
    return _request(
        account,
        user,
        "content_hashtags",
        "Suggest hashtags",
        {
            "caption": caption[:MAX_CAPTION],
            "existing": existing,
            "platform": platform,
            "room": room,
        },
    )


# --- Ideas from results ------------------------------------------------------------------


def idea_sources(account, now=None) -> list[dict]:
    """The best-selling posts of the last 90 days that have enough tracked chats to judge
    (the sales report's ranking: confirmed credits only, at least ``sales.MIN_CHATS``)."""
    from apps.conversations import reporting

    from . import sales

    report = sales.report(account, reporting.last_days(IDEA_DAYS, now))
    if not report:
        return []
    out = []
    for row in report["ranked"][:IDEA_SOURCES]:
        post, c = row["post"], row["confirmed"]
        out.append(
            {
                "id": post.pk,
                "caption": (post.caption or "")[:400],
                "media_type": post.media_type,
                "platforms": list(post.platforms or []),
                "chats": c["chats"],
                "leads": c["leads"],
                "paid_orders": c["paid_orders"],
            }
        )
    return out


def request_ideas(account, user, sources: list[dict] | None = None):
    from .models import ContentPost

    sources = idea_sources(account) if sources is None else sources
    if not sources:
        raise AiPlusError("There aren't enough results to learn from yet.")
    return _request(
        account,
        user,
        "content_ideas",
        "Post ideas from results",
        {
            "sources": sources,
            "media_types": [v for v, _ in ContentPost.MediaType.choices],
        },
    )


def save_idea(account, draft_id, index: int):
    """Add one suggested idea to Planning as an idea. Returns the plan item, or None."""
    from django.utils import timezone

    from apps.ai.models import AIDraft

    from .models import ContentPlanItem, ContentPost

    draft = AIDraft.objects.filter(
        pk=draft_id,
        account=account,
        kind=AIDraft.Kind.CONTENT_IDEAS,
        status__in=[AIDraft.Status.READY, AIDraft.Status.USED],
    ).first()
    if draft is None:
        return None
    ideas = (draft.result or {}).get("ideas") or []
    if not 0 <= index < len(ideas):
        return None
    idea = ideas[index]
    source = ContentPost.objects.filter(account=account, pk=idea["based_on"]).first()
    item = ContentPlanItem.objects.create(
        account=account,
        topic=idea["topic"][:255],
        objective=idea.get("angle", ""),
        media_type=idea.get("media_type") or "image",
        platforms=list(source.platforms or []) if source else [],
        plan_status=ContentPlanItem.PlanStatus.IDEA,
        notes=f"Suggested by AI from post {idea['based_on']}'s results.",
    )
    if draft.status != AIDraft.Status.USED:
        draft.status, draft.used_at = AIDraft.Status.USED, timezone.now()
        draft.save(update_fields=["status", "used_at"])
    return item


# --- Recording what a post used ----------------------------------------------------------


def parse_assist(raw) -> list[int]:
    ids = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))
    return ids[:MAX_ASSIST]


def record_assist(post, draft_ids: list[int]) -> None:
    """Mark ``post`` AI-assisted for the rewrites and hashtag suggestions it used.

    Only finished drafts of these kinds from the post's own workspace count. A rewrite is
    kept once as an accepted caption, like a picked caption suggestion.
    """
    from django.utils import timezone

    from apps.ai.models import AIDraft

    from .models import ContentCaption

    if not draft_ids:
        return
    drafts = list(
        AIDraft.objects.filter(
            pk__in=draft_ids,
            account_id=post.account_id,
            kind__in=[AIDraft.Kind.CONTENT_REWRITE, AIDraft.Kind.CONTENT_HASHTAGS],
            status__in=[AIDraft.Status.READY, AIDraft.Status.USED],
        )
    )
    if not drafts:
        return
    for draft in drafts:
        if draft.kind == AIDraft.Kind.CONTENT_REWRITE:
            body = str((draft.result or {}).get("after") or "")
            if (
                body
                and not ContentCaption.objects.filter(
                    post=post, body=body, accepted=True
                ).exists()
            ):
                ContentCaption.objects.create(
                    post=post, prompt=draft.prompt, body=body, accepted=True
                )
        if draft.status != AIDraft.Status.USED:
            draft.status, draft.used_at = AIDraft.Status.USED, timezone.now()
            draft.save(update_fields=["status", "used_at"])
    if not post.ai_assisted:
        post.ai_assisted = True
        post.save(update_fields=["ai_assisted"])
