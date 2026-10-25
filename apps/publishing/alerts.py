"""Tell a business's Owners and Admins when a channel didn't publish, once per episode.

An episode starts when a channel (a ContentPostVariant) lands in FAILED or RECONCILING and
ends only when that channel is published or cancelled. Inside one episode each kind of
problem is reported once:

- ``failed``: the channel did not publish. Sent after the failure has settled for
  ``SETTLE`` minutes, so automatic retries (about 7 minutes of backoff) finish first.
- ``reconciling``: nobody knows whether the channel went live. A different message: the
  business must check before anything is retried.

Retrying a failed channel, by hand or automatically, stays in the same episode: an owner
who was told and retried is not emailed again when the retry fails too. Once the channel
publishes or is cancelled the episode closes, so a later problem is reported afresh.

The sweep reads current state rather than hooking every path that sets a status, so no
path can be missed and running it twice sends nothing twice. Only problems from the last
``LOOKBACK`` are considered, so turning this on does not email about old failures.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import ContentPostVariant

logger = logging.getLogger(__name__)

ALERTS_KEY = (
    "problem_alerts"  # variant.metadata: {"failed": iso time, "reconciling": ...}
)
SETTLE = timedelta(minutes=15)
LOOKBACK = timedelta(days=3)
BATCH = 200

_PLATFORM_LABELS = {"facebook": "Facebook", "instagram": "Instagram"}
_CLOSED = (ContentPostVariant.Status.PUBLISHED, ContentPostVariant.Status.CANCELLED)


def _label(variant) -> str:
    return _PLATFORM_LABELS.get(variant.platform, variant.platform.title())


def _quote(post) -> str:
    caption = " ".join((post.caption or "").split())
    return (
        f"“{caption[:60]}{'…' if len(caption) > 60 else ''}”"
        if caption
        else "your post"
    )


def _fix_hint(variant) -> str:
    kind = variant.last_error_type
    if kind == ContentPostVariant.ErrorType.PERMISSION:
        return (
            f"Reconnect {_label(variant)} on the Channels page in Content Studio, "
            "then retry the post."
        )
    if kind == ContentPostVariant.ErrorType.VALIDATION:
        return "Fix what the reason says (for example the media or the caption), then retry."
    if kind == ContentPostVariant.ErrorType.RATE_LIMIT:
        return f"{_label(variant)} asked us to slow down. Retry it in a little while."
    return "Open the post to retry it."


def message_for(variant, kind: str) -> dict:
    """Subject, text and link for a ``kind`` alert about ``variant``."""
    post = variant.post
    channel = _label(variant)
    if kind == "reconciling":
        return {
            "subject": f"Check whether your {channel} post went live",
            "text": (
                f"We couldn't confirm whether {_quote(post)} was published to {channel}. "
                "Akilent won't retry it on its own, so it can't post twice.\n\n"
                f"Look at your {channel} page, then in Akilent mark it as published, "
                "or retry it if it isn't there."
            ),
            "path": reverse("publishing:reconciliation-queue"),
        }
    reason = " ".join((variant.last_error or "No reason was given.").split())[:300]
    return {
        "subject": f"Your {channel} post didn't publish",
        "text": (
            f"{_quote(post)} couldn't be published to {channel}.\n\n"
            f"Reason: {reason}\n\n{_fix_hint(variant)}"
        ),
        "path": reverse("publishing:post-detail", args=[post.pk]),
    }


def _close_episodes() -> int:
    """Forget sent alerts for channels that have since published or been cancelled."""
    closed = 0
    for variant in ContentPostVariant.objects.filter(
        status__in=_CLOSED, metadata__has_key=ALERTS_KEY
    )[:BATCH]:
        with transaction.atomic():
            fresh = ContentPostVariant.objects.select_for_update().get(pk=variant.pk)
            if fresh.status not in _CLOSED:
                continue
            fresh.metadata = {
                k: v for k, v in (fresh.metadata or {}).items() if k != ALERTS_KEY
            }
            fresh.save(update_fields=["metadata"])
            closed += 1
    return closed


def _candidates(now):
    recent = Q(last_error_at__gte=now - LOOKBACK) | Q(
        last_error_at__isnull=True, updated_at__gte=now - LOOKBACK
    )
    failed = Q(status=ContentPostVariant.Status.FAILED) & (
        Q(last_error_at__lte=now - SETTLE)
        | Q(last_error_at__isnull=True, updated_at__lte=now - SETTLE)
    )
    reconciling = Q(status=ContentPostVariant.Status.RECONCILING)
    return (
        ContentPostVariant.objects.filter(recent)
        .filter(failed | reconciling)
        .order_by("pk")
        .values_list("pk", flat=True)[:BATCH]
    )


def _claim(pk: int, now) -> tuple[ContentPostVariant, str] | None:
    """Lock the channel, recheck it, and record the alert as sent. None when nothing is due.

    Recorded before the email goes out: a crash after this point loses one email rather
    than sending it again on every sweep.
    """
    with transaction.atomic():
        variant = (
            ContentPostVariant.objects.select_for_update()
            .select_related("post__account")
            .filter(pk=pk)
            .first()
        )
        if variant is None:
            return None
        if variant.status == ContentPostVariant.Status.RECONCILING:
            kind = "reconciling"
        elif variant.status == ContentPostVariant.Status.FAILED:
            kind = "failed"
        else:
            return None
        sent = dict((variant.metadata or {}).get(ALERTS_KEY) or {})
        if kind in sent:
            return None
        sent[kind] = now.isoformat()
        variant.metadata = {**(variant.metadata or {}), ALERTS_KEY: sent}
        variant.save(update_fields=["metadata"])
        return variant, kind


def send_due_alerts(now=None) -> dict:
    """One sweep: close finished episodes, then email each new problem once."""
    from apps.accounts.notifications import notify_team

    now = now or timezone.now()
    closed = _close_episodes()
    sent = skipped = 0
    for pk in list(_candidates(now)):
        claimed = _claim(pk, now)
        if claimed is None:
            skipped += 1
            continue
        variant, kind = claimed
        account = variant.post.account
        msg = message_for(variant, kind)
        try:
            notify_team(
                account,
                to="owners",
                subject=msg["subject"],
                text=msg["text"],
                path=msg["path"],
                reason=(
                    "You're getting this because you're an Owner or Admin of "
                    f"{account.company_name} in Akilent."
                ),
            )
            sent += 1
        except Exception:
            logger.exception(
                "publishing alert failed for variant=%s kind=%s", variant.pk, kind
            )
    return {"sent": sent, "skipped": skipped, "closed": closed}
