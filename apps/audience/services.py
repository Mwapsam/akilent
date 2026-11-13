"""Write and read audience signals for Instagram and Facebook comments (spec §4).

``record_safely`` is the only call the webhook pipeline makes. It never raises:
audience signals are measurement, and must never break moderation, triggers or
storage of the comment itself.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction

from . import classifier
from .models import AudienceSignal, AudienceSignalCorrection, AudienceSignalRun, Channel
from .taxonomy import is_valid, precedence_rank

logger = logging.getLogger(__name__)

Source = AudienceSignalRun.Source


def enabled() -> bool:
    return getattr(settings, "AUDIENCE_SIGNALS_ENABLED", True)


def text_hash(body: str) -> str:
    return hashlib.sha256((body or "").strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Which comment is this?
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Ref:
    channel: str
    account_id: int
    fk: dict  # {"ig_comment": c} or {"fb_comment": c}


def _ref(comment) -> _Ref:
    from apps.facebook.models.comment import FacebookComment
    from apps.instagram.models.comment import Comment

    if isinstance(comment, Comment):
        return _Ref(
            Channel.INSTAGRAM,
            comment.thread.instagram_account.account_id,
            {"ig_comment": comment},
        )
    if isinstance(comment, FacebookComment):
        return _Ref(
            Channel.FACEBOOK, comment.thread.account_id, {"fb_comment": comment}
        )
    raise TypeError(f"not a comment: {type(comment).__name__}")


def _is_inbound(comment) -> bool:
    return comment.direction == "inbound"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def classify_comment(comment) -> AudienceSignalRun | None:
    """Store the rule signals for ``comment``'s current text; idempotent.

    Returns the run (new or existing), or ``None`` when the comment isn't classified
    (switched off, outbound, or no text).
    """
    if not enabled() or not _is_inbound(comment) or not (comment.body or "").strip():
        return None
    ref = _ref(comment)
    digest = text_hash(comment.body)
    lookup = {
        **ref.fk,
        "source": Source.RULE,
        "classifier_version": classifier.CLASSIFIER_VERSION,
        "text_hash": digest,
    }
    existing = AudienceSignalRun.objects.filter(**lookup).first()
    if existing:
        return existing

    result = classifier.classify(comment.body)
    primary = result.primary
    try:
        with transaction.atomic():
            run = AudienceSignalRun.objects.create(
                account_id=ref.account_id,
                channel=ref.channel,
                language=result.language,
                occurred_at=comment.timestamp,
                **lookup,
            )
            AudienceSignal.objects.bulk_create(
                AudienceSignal(
                    run=run,
                    category=s.category,
                    subcategory=s.subcategory,
                    is_primary=s is primary,
                    matched_terms=list(s.terms),
                )
                for s in result.signals
            )
    except IntegrityError:
        # A concurrent delivery of the same webhook got there first.
        return AudienceSignalRun.objects.filter(**lookup).first()
    return run


def record_safely(comment) -> None:
    """``classify_comment`` for the webhook pipeline: logs, never raises."""
    try:
        classify_comment(comment)
    except Exception:
        logger.exception(
            "audience: could not classify comment %s", getattr(comment, "pk", None)
        )


# ---------------------------------------------------------------------------
# Reading: the comment's current signals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentSignal:
    category: str
    subcategory: str
    source: str  # "rule", "ai" or "owner"
    is_primary: bool = False


def _pick(runs: list, source: str, digest: str, *, fallback: bool):
    """The newest run of ``source`` for this text (``runs`` are newest first)."""
    mine = [r for r in runs if r.source == source]
    run = next((r for r in mine if r.text_hash == digest), None)
    if run is None and fallback and mine:
        run = mine[0]
    return run


def _base(runs: list, digest: str) -> dict[tuple[str, str], str]:
    """Rule signals, with AI filling in only where the rules found nothing specific."""
    signals: dict[tuple[str, str], str] = {}
    rule_run = _pick(runs, Source.RULE, digest, fallback=True)
    if rule_run:
        for s in rule_run.signals.all():
            signals[(s.category, s.subcategory)] = Source.RULE
    ai_run = _pick(runs, Source.AI, digest, fallback=False)
    if ai_run and not any(cat != "general" for (cat, _sub) in signals):
        # Where the rules did find something, the rule result stands; the AI writer
        # records any disagreement on its own run.
        ai = [(s.category, s.subcategory) for s in ai_run.signals.all()]
        if any(cat != "general" for (cat, _sub) in ai):
            signals = {k: Source.AI for k in ai if k[0] != "general"}
    return signals


def _resolve(runs: list, corrections: list, digest: str) -> list[CurrentSignal]:
    signals = _base(runs, digest)
    mine = [c for c in corrections if c.text_hash == digest]
    for c in mine:
        if c.action == AudienceSignalCorrection.Action.REJECT:
            signals = {
                k: v
                for k, v in signals.items()
                if not (
                    k[0] == c.category and (not c.subcategory or k[1] == c.subcategory)
                )
            }
    for c in mine:
        if c.action == AudienceSignalCorrection.Action.ADD:
            signals.setdefault((c.category, c.subcategory), "owner")
    ordered = sorted(signals.items(), key=lambda kv: precedence_rank(kv[0][0]))
    return [
        CurrentSignal(cat, sub, src, is_primary=(i == 0))
        for i, ((cat, sub), src) in enumerate(ordered)
    ]


def current_signals(comment) -> list[CurrentSignal]:
    """Resolve rules, AI and owner corrections into one list (spec §4).

    1. Rule signals from the latest rule run for the current text (else the latest).
    2. AI signals only where the rules found nothing specific (just "general"); where
       the rules did, the rule result stands and AI is ignored.
    3. Owner corrections for the current text: reject removes, add inserts. Always win.
    4. Primary recomputed by precedence.
    """
    return current_signals_for([comment]).get(_key(comment), [])


def _key(comment) -> tuple[str, int]:
    from apps.facebook.models.comment import FacebookComment

    channel = (
        Channel.FACEBOOK if isinstance(comment, FacebookComment) else Channel.INSTAGRAM
    )
    return channel, comment.pk


def current_signals_for(comments) -> dict[tuple[str, int], list[CurrentSignal]]:
    """``current_signals`` for many comments (Instagram and Facebook mixed). Keyed by
    ``(channel, comment pk)``."""
    return signals_for_rows((*_key(c), c.body) for c in comments)


def signals_for_rows(rows) -> dict[tuple[str, int], list[CurrentSignal]]:
    """Current signals for ``(channel, comment pk, body)`` rows, in four queries."""
    bodies: dict[str, dict[int, str]] = {Channel.INSTAGRAM: {}, Channel.FACEBOOK: {}}
    for channel, pk, body in rows:
        bodies[channel][pk] = body or ""
    out: dict[tuple[str, int], list[CurrentSignal]] = {}
    for channel, fk in (
        (Channel.INSTAGRAM, "ig_comment"),
        (Channel.FACEBOOK, "fb_comment"),
    ):
        found = bodies[channel]
        if not found:
            continue
        runs: dict[int, list] = {}
        for run in (
            AudienceSignalRun.objects.filter(**{f"{fk}__in": list(found)})
            .prefetch_related("signals")
            .order_by("-created_at", "-pk")
        ):
            runs.setdefault(getattr(run, f"{fk}_id"), []).append(run)
        corrections: dict[int, list] = {}
        for corr in AudienceSignalCorrection.objects.filter(
            **{f"{fk}__in": list(found)}
        ):
            corrections.setdefault(getattr(corr, f"{fk}_id"), []).append(corr)
        for pk, body in found.items():
            out[(channel, pk)] = _resolve(
                runs.get(pk, []), corrections.get(pk, []), text_hash(body)
            )
    return out


# ---------------------------------------------------------------------------
# Owner corrections
# ---------------------------------------------------------------------------


def set_correction(comment, category: str, subcategory: str, action: str, *, user=None):
    """Record an owner's add/reject for ``comment``'s current text (latest wins)."""
    if not is_valid(category, subcategory):
        raise ValueError(f"unknown category {category}/{subcategory}")
    if action not in AudienceSignalCorrection.Action.values:
        raise ValueError(f"unknown action {action}")
    ref = _ref(comment)
    correction, _ = AudienceSignalCorrection.objects.update_or_create(
        **ref.fk,
        category=category,
        subcategory=subcategory,
        defaults={
            "account_id": ref.account_id,
            "action": action,
            "text_hash": text_hash(comment.body),
            "created_by": user,
        },
    )
    return correction


def set_topics(comment, chosen, *, user=None) -> list[CurrentSignal]:
    """The owner ticked which categories a comment is about (whole categories).

    A category the classifier found and the owner unticked gets a reject; one the owner
    ticked that isn't there gets an add. Ticking a category back drops the reject
    instead of piling up an add on top of it. Returns the comment's signals afterwards.
    """
    chosen = {c for c in chosen if is_valid(c)}
    ref = _ref(comment)
    digest = text_hash(comment.body)
    runs = list(
        AudienceSignalRun.objects.filter(**ref.fk)
        .prefetch_related("signals")
        .order_by("-created_at", "-pk")
    )
    found = {cat for (cat, _sub) in _base(runs, digest)} - {"general"}
    current = {s.category for s in current_signals(comment)} - {"general"}
    corrections = AudienceSignalCorrection.objects.filter(**ref.fk)
    with transaction.atomic():
        for cat in current - chosen:
            # Drop any owner adds for it, then reject what the classifier found.
            corrections.filter(category=cat, action="add").delete()
            if cat in found:
                set_correction(comment, cat, "", "reject", user=user)
        for cat in chosen - current:
            if cat in found:
                corrections.filter(category=cat, action="reject").delete()
            else:
                set_correction(comment, cat, "", "add", user=user)
    return current_signals(comment)
