"""What people are saying on a post, and what came of it (spec §7, §9).

Counts are **distinct commenters** (one person asking five times counts once), with
comment counts beside them. Deleted comments are left out, and hidden ones are never
shown as examples.

Outcomes use **comment-level evidence only**: a commenter is "linked" when a private
reply to their comment opened a conversation (``CommentThread.conversation``, the same
event that records a ``ig_comment`` / ``fb_comment`` entry point). A lead or paid order
counts when its attribution names *this post* as the source, which only an explicit
link can do. Post-level results (tracked-link chats and so on) stay on the post's
results card. Every row says how many commenters were linked and how many weren't, and
keeps "still talking" apart from "no recorded outcome", so a zero can be read honestly.
"""

from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from . import services
from .models import Channel
from .taxonomy import (
    OWNER_CATEGORIES,
    Category,
    label,
    precedence_rank,
    sub_label,
)

EXAMPLES = 3
# A linked conversation with no lead or order yet, active this recently, is "still
# talking" rather than "no recorded outcome".
IN_PROGRESS_DAYS = 14
# Categories where an unanswered root comment is worth pointing at.
_NEEDS_ANSWER = {Category.BUYING_INTENT, Category.PRODUCT_ENQUIRY, Category.COMPLAINT}


@dataclass
class Example:
    author: str
    body: str
    channel: str
    timestamp: datetime.datetime


@dataclass
class Outcome:
    linked: int = 0  # commenters whose comment opened a conversation
    unlinked: int = 0  # commenters with no conversation from their comment
    leads: int = 0
    orders: int = 0  # paid orders
    revenue: list[dict] = field(default_factory=list)  # [{currency, total}]
    in_progress: int = 0  # linked conversations, no outcome yet, recently active
    no_outcome: int = 0  # linked conversations, no outcome, gone quiet


@dataclass
class CategoryRow:
    category: str
    label: str
    commenters: int
    comments: int
    subs: list[tuple[str, int]]  # (label, commenters), most first
    examples: list[Example]
    unanswered: int | None  # commenters nobody answered (questions, intent, complaints)
    outcome: Outcome
    action_label: str = ""
    action_url: str = ""


@dataclass
class PostAudience:
    rows: list[CategoryRow]
    commenters: int  # everyone who commented (not deleted)
    comments: int
    specific: int  # commenters who said something beyond chat
    linked: int  # commenters whose comment opened a conversation
    comments_url: str

    @property
    def unlinked(self) -> int:
        return self.commenters - self.linked


@dataclass
class _Row:
    channel: str
    pk: int
    body: str
    author: str
    commenter: tuple[str, int]
    timestamp: datetime.datetime
    hidden: bool
    is_root: bool
    thread_key: tuple[str, int]
    conversation_id: int | None
    variant_id: int | None


def _rows_for_variants(variant_ids: list[int]) -> tuple[list[_Row], set]:
    """Inbound, not-deleted comments on these Content Studio channels, newest first,
    plus the threads somebody has answered (a public reply or a private one)."""
    from apps.facebook.models.comment import FacebookComment
    from apps.instagram.models.comment import Comment

    rows: list[_Row] = []
    answered: set[tuple[str, int]] = set()
    for channel, model, contact in (
        (Channel.INSTAGRAM, Comment, "instagram_contact"),
        (Channel.FACEBOOK, FacebookComment, "facebook_contact"),
    ):
        base = model.objects.filter(thread__content_variant_id__in=variant_ids)
        answered |= {
            (channel, t)
            for t in base.filter(direction="outbound").values_list(
                "thread_id", flat=True
            )
        }
        for c in (
            base.filter(direction="inbound")
            .exclude(moderation_state="deleted")
            .select_related("thread", contact)
            .order_by("-timestamp", "-pk")
        ):
            person = getattr(c, contact)
            if channel == Channel.INSTAGRAM:
                author = f"@{person.username}" if person.username else person.name
            else:
                author = person.name
            rows.append(
                _Row(
                    channel=channel,
                    pk=c.pk,
                    body=c.body or "",
                    author=author or "Someone",
                    commenter=(channel, person.pk),
                    timestamp=c.timestamp,
                    hidden=c.is_hidden or c.moderation_state == "hidden",
                    is_root=c.comment_id == c.thread.comment_id,
                    thread_key=(channel, c.thread_id),
                    conversation_id=c.thread.conversation_id,
                    variant_id=c.thread.content_variant_id,
                )
            )
    rows.sort(key=lambda r: (r.timestamp, r.pk), reverse=True)
    return rows, answered


def _outcomes(post, conversation_ids: set[int]) -> dict[int, dict]:
    """Per conversation: lead ids, paid orders and recent activity, counting only
    attributions that name this post (explicit evidence)."""
    from apps.conversations.models import Conversation, ConversationAttribution

    if not conversation_ids:
        return {}
    out: dict[int, dict] = {
        pk: {"leads": set(), "orders": {}, "active": False} for pk in conversation_ids
    }
    since = timezone.now() - datetime.timedelta(days=IN_PROGRESS_DAYS)
    for pk, last in Conversation.objects.filter(pk__in=conversation_ids).values_list(
        "pk", "last_message_at"
    ):
        out[pk]["active"] = bool(last and last >= since)
    for a in ConversationAttribution.objects.filter(
        conversation_id__in=conversation_ids, source_post=post
    ).select_related("order"):
        if a.lead_id:
            out[a.conversation_id]["leads"].add(a.lead_id)
        if a.order_id and a.order.status == "paid":
            out[a.conversation_id]["orders"][a.order_id] = (
                a.order.currency,
                a.order.total or Decimal("0"),
            )
    return out


def _action(category: str, comments_url: str) -> tuple[str, str]:
    topic_url = f"{comments_url}&topic={category}"
    if category == Category.BUYING_INTENT:
        return "Answer them", topic_url
    if category == Category.PRODUCT_ENQUIRY:
        return "Add the answers to Knowledge", reverse("settings-ai-knowledge")
    if category == Category.COMPLAINT:
        return "Look into them", topic_url
    if category == Category.PRAISE:
        return "See who loved it", topic_url
    if category == Category.CONTENT_FEEDBACK:
        return "Plan a post", reverse("publishing:post-new")
    if category == Category.SPAM_ABUSE:
        return "Review them", topic_url
    return "", ""


def post_audience(post) -> PostAudience | None:
    """The "What people are saying" summary for a Content Studio post, or None when
    nobody has commented on it."""
    variant_ids = list(
        post.variants.filter(
            platform__in=(Channel.INSTAGRAM, Channel.FACEBOOK)
        ).values_list("pk", flat=True)
    )
    if not variant_ids:
        return None
    rows, answered = _rows_for_variants(variant_ids)
    if not rows:
        return None
    signals = services.signals_for_rows((r.channel, r.pk, r.body) for r in rows)

    # The channel with most comments is where "see them" links go.
    busiest = Counter(r.variant_id for r in rows).most_common(1)[0][0]
    comments_url = reverse("conversations:comments") + f"?variant={busiest}&filter=all"

    commenters: dict[str, set] = {}
    comment_count: Counter = Counter()
    subs: dict[str, dict[str, set]] = {}
    examples: dict[str, list[Example]] = {}
    unanswered: dict[str, set] = {}
    linked_convs: dict[str, dict[tuple, int]] = {}  # category -> commenter -> conv
    everyone: set = set()
    specific: set = set()
    linked_people: set = set()

    for r in rows:
        everyone.add(r.commenter)
        if r.is_root and r.conversation_id:
            linked_people.add(r.commenter)
        cats: dict[str, list[str]] = {}
        for s in signals.get((r.channel, r.pk), []):
            cats.setdefault(s.category, []).append(s.subcategory)
        for cat, cat_subs in cats.items():
            commenters.setdefault(cat, set()).add(r.commenter)
            comment_count[cat] += 1
            for sub in cat_subs:
                if sub:
                    subs.setdefault(cat, {}).setdefault(sub, set()).add(r.commenter)
            if cat != Category.GENERAL:
                specific.add(r.commenter)
            if not r.hidden and r.body.strip():
                bucket = examples.setdefault(cat, [])
                if len(bucket) < EXAMPLES:
                    bucket.append(Example(r.author, r.body, r.channel, r.timestamp))
            if r.is_root:
                if r.conversation_id:
                    linked_convs.setdefault(cat, {})[r.commenter] = r.conversation_id
                elif cat in _NEEDS_ANSWER and r.thread_key not in answered:
                    unanswered.setdefault(cat, set()).add(r.commenter)

    all_convs = {c for per in linked_convs.values() for c in per.values()}
    outcomes = _outcomes(post, all_convs)

    result_rows = []
    for cat in sorted(commenters, key=precedence_rank):
        if cat == Category.GENERAL:
            continue
        people = commenters[cat]
        convs = set(linked_convs.get(cat, {}).values())
        leads: set = set()
        orders: dict = {}
        in_progress = no_outcome = 0
        for conv in convs:
            o = outcomes.get(conv, {"leads": set(), "orders": {}, "active": False})
            leads |= o["leads"]
            orders.update(o["orders"])
            if not o["leads"] and not o["orders"]:
                if o["active"]:
                    in_progress += 1
                else:
                    no_outcome += 1
        revenue: dict[str, Decimal] = {}
        for currency, total in orders.values():
            revenue[currency] = revenue.get(currency, Decimal("0")) + total
        linked = len(linked_convs.get(cat, {}))
        action_label, action_url = _action(cat, comments_url)
        result_rows.append(
            CategoryRow(
                category=cat,
                label=label(cat),
                commenters=len(people),
                comments=comment_count[cat],
                subs=sorted(
                    (
                        (sub_label(sub), len(who))
                        for sub, who in subs.get(cat, {}).items()
                    ),
                    key=lambda x: -x[1],
                ),
                examples=examples.get(cat, []),
                unanswered=len(unanswered.get(cat, set()))
                if cat in _NEEDS_ANSWER
                else None,
                outcome=Outcome(
                    linked=linked,
                    unlinked=len(people) - linked,
                    leads=len(leads),
                    orders=len(orders),
                    revenue=[
                        {"currency": c, "total": t} for c, t in sorted(revenue.items())
                    ],
                    in_progress=in_progress,
                    no_outcome=no_outcome,
                ),
                action_label=action_label,
                action_url=action_url,
            )
        )
    return PostAudience(
        rows=result_rows,
        commenters=len(everyone),
        comments=len(rows),
        specific=len(specific),
        linked=len(linked_people),
        comments_url=comments_url,
    )


# ---------------------------------------------------------------------------
# Comments list: topics on each comment, and the topic filter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Topic:
    category: str
    label: str
    detail: str  # subcategory wording, "" when none
    source: str  # rule / ai / owner


def topics_for(rows) -> dict[tuple[str, int], list[Topic]]:
    """Owner-facing topics for ``(channel, pk, body)`` rows: one per category, primary
    first, "Chat" left out (it means nothing specific was said)."""
    out: dict[tuple[str, int], list[Topic]] = {}
    for key, signals in services.signals_for_rows(rows).items():
        seen: dict[str, Topic] = {}
        for s in signals:
            if s.category == Category.GENERAL:
                continue
            if s.category in seen:
                if s.subcategory and not seen[s.category].detail:
                    seen[s.category] = Topic(
                        s.category,
                        label(s.category),
                        sub_label(s.subcategory),
                        s.source,
                    )
                continue
            seen[s.category] = Topic(
                s.category,
                label(s.category),
                sub_label(s.subcategory) if s.subcategory else "",
                s.source,
            )
        out[key] = list(seen.values())
    return out


TOPIC_FILTERS: tuple[tuple[str, str], ...] = tuple(
    (c, label(c)) for c in OWNER_CATEGORIES
)


def topic_exists(channel: str, topic: str):
    """An ``Exists`` filter for comment threads (Instagram ``CommentThread`` or
    ``FacebookCommentThread`` querysets) whose root comment is about ``topic``.

    Close to ``current_signals`` in SQL: any classifier or owner-added signal for the
    category, minus a whole-category owner reject. The page re-checks each comment
    with the exact resolution before showing it.
    """
    from django.db.models import Exists, OuterRef

    from .models import AudienceSignal, AudienceSignalCorrection

    fk = "ig_comment" if channel == Channel.INSTAGRAM else "fb_comment"
    root = {
        f"{fk}__thread": OuterRef("pk"),
        f"{fk}__comment_id": OuterRef("comment_id"),
    }
    found = AudienceSignal.objects.filter(
        **{f"run__{k}": v for k, v in root.items()}, category=topic
    )
    added = AudienceSignalCorrection.objects.filter(
        **root, category=topic, action="add"
    )
    rejected = AudienceSignalCorrection.objects.filter(
        **root, category=topic, subcategory="", action="reject"
    )
    return (Exists(found) | Exists(added)) & ~Exists(rejected)
