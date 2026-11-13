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


def _latest_run(ref: _Ref, source: str, digest: str, *, fallback: bool):
    runs = AudienceSignalRun.objects.filter(**ref.fk, source=source).order_by(
        "-created_at", "-pk"
    )
    run = runs.filter(text_hash=digest).first()
    if run is None and fallback:
        run = runs.first()
    return run


def current_signals(comment) -> list[CurrentSignal]:
    """Resolve rules, AI and owner corrections into one list (spec §4).

    1. Rule signals from the latest rule run for the current text (else the latest).
    2. AI signals only where the rules found nothing specific (just "general"); where
       the rules did, the rule result stands and AI is ignored.
    3. Owner corrections for the current text: add inserts, reject removes. Always win.
    4. Primary recomputed by precedence.
    """
    ref = _ref(comment)
    digest = text_hash(comment.body)

    signals: dict[tuple[str, str], str] = {}
    rule_run = _latest_run(ref, Source.RULE, digest, fallback=True)
    if rule_run:
        for s in rule_run.signals.all():
            signals[(s.category, s.subcategory)] = Source.RULE

    ai_run = _latest_run(ref, Source.AI, digest, fallback=False)
    rules_said_something = any(cat != "general" for (cat, _sub) in signals)
    if ai_run and not rules_said_something:
        # AI only fills in where the rules found nothing specific. Where they did, the
        # rule result stands; the AI writer records any disagreement on its own run.
        ai = {(s.category, s.subcategory) for s in ai_run.signals.all()}
        if any(cat != "general" for (cat, _sub) in ai):
            signals = {k: Source.AI for k in ai if k[0] != "general"}

    corrections = AudienceSignalCorrection.objects.filter(**ref.fk, text_hash=digest)
    for c in corrections:
        if c.action == AudienceSignalCorrection.Action.REJECT:
            signals = {
                k: v
                for k, v in signals.items()
                if not (
                    k[0] == c.category and (not c.subcategory or k[1] == c.subcategory)
                )
            }
    for c in corrections:
        if c.action == AudienceSignalCorrection.Action.ADD:
            signals[(c.category, c.subcategory)] = "owner"

    ordered = sorted(signals.items(), key=lambda kv: precedence_rank(kv[0][0]))
    return [
        CurrentSignal(cat, sub, src, is_primary=(i == 0))
        for i, ((cat, sub), src) in enumerate(ordered)
    ]


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
