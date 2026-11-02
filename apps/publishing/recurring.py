"""Recurring posts (Phase 2b, ``cs_recurring``).

A rule repeats a post every ``every_days`` (7 or more). A day before each run, a new
post is made from the original through the composer's own path: the same readiness
check the Schedule button runs, then ``composer.save_new_or_existing``. So every copy
is checked and, in a workspace that reviews posts, sent for review rather than
scheduled. Nothing is ever published without that.

- Alternate captions are used in turn. An Instagram copy whose caption and media are
  the same as the last copy's is held as a draft: Instagram treats repeated content
  as spam, so it needs a change first.
- A copy that fails readiness is skipped, and the run says why. Owners and Admins are
  emailed.
- Each run is recorded once (``RecurrenceRun``, unique per rule and time), so a task
  that runs twice makes one post.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from django.db import transaction
from django.http import QueryDict
from django.utils import timezone

from apps.accounts.business_hours import workspace_zone as _tz

from .models import (
    ContentPost,
    PublishingSettings,
    RecurrenceRule,
    RecurrenceRun,
    TrackedLink,
)

logger = logging.getLogger(__name__)

FLAG = "cs_recurring"
MIN_DAYS = 7
MAX_DAYS = 365
MAX_CAPTIONS = 10
LEAD = datetime.timedelta(days=1)  # copies are made this long before they go out
TOO_LATE = datetime.timedelta(hours=1)  # a run missed by more than this is skipped
HELD_REASON = (
    "Instagram needs a changed caption or media for a repeat. Add another caption to "
    "this repeat, or edit the draft."
)
Run = RecurrenceRun.Outcome


class RecurrenceError(ValueError):
    """The rule isn't valid; the message is for the person."""


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def _source_time(post: ContentPost):
    published = (
        post.variants.filter(published_at__isnull=False)
        .order_by("published_at")
        .values_list("published_at", flat=True)
        .first()
    )
    return published or post.scheduled_at


def create_rule(post, user, every_days, first_run_at, captions=()) -> RecurrenceRule:
    try:
        every_days = int(every_days)
    except (TypeError, ValueError) as exc:
        raise RecurrenceError("Choose how many days apart.") from exc
    if not MIN_DAYS <= every_days <= MAX_DAYS:
        raise RecurrenceError(f"Repeat every {MIN_DAYS} to {MAX_DAYS} days.")
    if first_run_at is None or first_run_at <= timezone.now() + LEAD:
        raise RecurrenceError("Start the repeat more than a day from now.")
    source_time = _source_time(post)
    if source_time and first_run_at < source_time + datetime.timedelta(days=MIN_DAYS):
        raise RecurrenceError(
            f"Start the repeat at least {MIN_DAYS} days after the post goes out."
        )
    cleaned = [c.strip() for c in captions if c and c.strip()]
    if len(cleaned) > MAX_CAPTIONS:
        raise RecurrenceError(f"Add up to {MAX_CAPTIONS} other captions.")
    return RecurrenceRule.objects.create(
        account=post.account,
        source=post,
        every_days=every_days,
        next_run_at=first_run_at,
        captions=cleaned,
        created_by=user,
    )


def _caption_for(rule: RecurrenceRule, index: int) -> tuple[str, bool]:
    """The caption for the ``index``-th run, and whether it is the original."""
    options = [rule.source.caption, *(rule.captions or [])]
    i = index % len(options)
    return options[i], i == 0


def _input_for(rule: RecurrenceRule, run_for, index: int):
    from .composer import ComposerInput

    source = rule.source
    caption, original = _caption_for(rule, index)
    variants = {
        v.platform: v
        for v in source.variants.filter(platform__in=source.platforms or [])
    }
    linked = set(
        TrackedLink.objects.filter(
            variant__in=variants.values(), disabled_at__isnull=True
        ).values_list("variant__platform", flat=True)
    )
    inp = ComposerInput(
        caption=caption,
        hashtags=list(source.hashtags or []),
        platforms=list(source.platforms or []),
        media_type=source.media_type,
        media_urls=list(source.media_urls or []),
        # Per-channel captions belong to the original caption.
        platform_captions=dict(source.platform_captions or {}) if original else {},
        scheduled_at=run_for,
        cta=dict(source.cta or {}),
        wa_links={p: p in linked for p in source.platforms or []},
        channel_options={p: dict(v.options or {}) for p, v in variants.items()},
    )
    data = QueryDict(mutable=True)
    for p in linked:
        data[f"wa_link_{p}"] = "on"
    return inp, data


def _previous(rule: RecurrenceRule) -> tuple[str, list]:
    """The caption and media of the last copy made (or the original)."""
    last = rule.runs.filter(post__isnull=False).select_related("post").first()
    post = last.post if last and last.post else rule.source
    return post.caption, list(post.media_urls or [])


def _next_time(rule: RecurrenceRule, after):
    """``every_days`` later at the same time on the business's clock."""
    tz = _tz(rule.account)
    local = timezone.localtime(after, tz)
    return datetime.datetime.combine(
        local.date() + datetime.timedelta(days=rule.every_days),
        local.time(),
        tzinfo=tz,
    )


def run_rule(rule_pk: int, now=None) -> RecurrenceRun | None:
    """Make the next copy if it is due (a day ahead). Returns the run, or None."""
    from . import readiness
    from .composer import save_new_or_existing

    now = now or timezone.now()
    with transaction.atomic():
        rule = (
            RecurrenceRule.objects.select_for_update()
            .select_related("source", "account")
            .filter(pk=rule_pk, active=True)
            .first()
        )
        if rule is None or rule.next_run_at > now + LEAD:
            return None
        run_for = rule.next_run_at
        rule.next_run_at = _next_time(rule, run_for)
        rule.save(update_fields=["next_run_at"])
        if rule.runs.filter(run_for=run_for).exists():
            return None
        index = rule.runs.count() + 1  # the original is run 0

        def record(outcome, post=None, reason=""):
            return RecurrenceRun.objects.create(
                rule=rule, run_for=run_for, outcome=outcome, post=post, reason=reason
            )

        if run_for < now - TOO_LATE:
            return record(
                Run.SKIPPED, reason="Its time had passed before it could be made."
            )

        account = rule.account
        inp, data = _input_for(rule, run_for, index)
        held = "instagram" in inp.platforms and (inp.caption, inp.media_urls) == (
            _previous(rule)
        )
        requires_review = PublishingSettings.requires_review_for(account)
        if not held:
            gate = readiness.SUBMIT_REVIEW if requires_review else readiness.SCHEDULE
            blockers = readiness.blocking(readiness.check(inp, account), gate)
            if blockers:
                return record(
                    Run.SKIPPED, reason=" ".join(i["message"] for i in blockers)
                )

        user = rule.created_by
        post = ContentPost(
            account=account,
            created_by=user,
            client_key=uuid.uuid5(
                uuid.NAMESPACE_URL, f"akilent:recur:{rule.pk}:{run_for.isoformat()}"
            ),
        )
        result = save_new_or_existing(
            post, inp, "draft" if held else "schedule", user, data
        )
        post = result.post
        if held:
            return record(Run.HELD, post, HELD_REASON)
        if result.review_blocked or requires_review:
            from .views import _submit_for_review

            _submit_for_review(post, user)
            return record(Run.IN_REVIEW, post)
        return record(Run.SCHEDULED, post)


def run_due(now=None) -> list[RecurrenceRun]:
    now = now or timezone.now()
    runs = []
    for pk in RecurrenceRule.objects.filter(
        active=True, next_run_at__lte=now + LEAD
    ).values_list("pk", flat=True):
        try:
            run = run_rule(pk, now)
        except Exception:
            logger.exception("recurring run failed for rule=%s", pk)
            continue
        if run is not None:
            runs.append(run)
            if run.outcome in (Run.SKIPPED, Run.HELD):
                _report(run)
    return runs


def _report(run: RecurrenceRun) -> None:
    from apps.accounts.notifications import notify_team

    rule = run.rule
    account = rule.account
    what = "held as a draft" if run.outcome == Run.HELD else "skipped"
    when = timezone.localtime(run.run_for, _tz(account))
    try:
        notify_team(
            account,
            to="owners",
            subject=f"A repeat of your post was {what}",
            text=(
                f"The repeat of “{(rule.source.caption or '')[:60]}” due "
                f"{when:%d %b at %H:%M} was {what}. {run.reason}"
            ),
            path="/publishing/recurring/",
            reason=(
                "You're getting this because you're an Owner or Admin of "
                f"{account.company_name} in Akilent."
            ),
        )
    except Exception:
        logger.exception("recurring report failed for run=%s", run.pk)
