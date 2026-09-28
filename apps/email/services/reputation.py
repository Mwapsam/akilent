"""Per-account sender-reputation circuit breaker.

Bounce and complaint events (from the SES SNS webhook) increment rolling
counters on :class:`~apps.email.models.SendReputation`; every send increments
``sent``. When the bounce or complaint rate over the trailing window crosses the
configured halt threshold the account's non-system sends are blocked until an
operator resets it. System mail (password resets, verification) is never blocked.

All functions are best-effort: a failure here must never crash a send or a
webhook — reputation accounting degrades, delivery does not.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)


def _thresholds():
    from apps.core.models import MailProviderSettings

    s = MailProviderSettings.load()
    return {
        "bounce_warn": s.reputation_bounce_warn,
        "bounce_halt": s.reputation_bounce_halt,
        "complaint_halt": s.reputation_complaint_halt,
        "min_volume": s.reputation_min_volume,
        "window_hours": s.reputation_window_hours,
    }


def _get_row(account, *, window_hours: int):
    from apps.email.models import SendReputation

    rep, _ = SendReputation.objects.get_or_create(account=account)
    # Roll the window over if it has aged out.
    if rep.window_started_at < timezone.now() - timedelta(hours=window_hours):
        rep.window_started_at = timezone.now()
        rep.sent = 0
        rep.bounced = 0
        rep.complained = 0
        if rep.state != rep.State.HALTED:
            # A halt persists across windows until an operator resets it; a
            # warn clears on a fresh window.
            rep.state = rep.State.OK
            rep.halted_reason = ""
        rep.save()
    return rep


def record_send(account) -> None:
    try:
        from apps.email.models import SendReputation

        th = _thresholds()
        _get_row(account, window_hours=th["window_hours"])
        SendReputation.objects.filter(account=account).update(sent=F("sent") + 1)
    except Exception:
        logger.exception(
            "record_send failed for account=%s", getattr(account, "pk", "?")
        )


def record_bounce(account, *, count: int = 1) -> None:
    _record_negative(account, field="bounced", count=count)


def record_complaint(account, *, count: int = 1) -> None:
    _record_negative(account, field="complained", count=count)


def _record_negative(account, *, field: str, count: int) -> None:
    try:
        from apps.email.models import SendReputation

        th = _thresholds()
        with transaction.atomic():
            _get_row(account, window_hours=th["window_hours"])
            SendReputation.objects.filter(account=account).update(
                **{field: F(field) + count}
            )
            rep = SendReputation.objects.select_for_update().get(account=account)
            _evaluate(rep, th)
    except Exception:
        logger.exception(
            "_record_negative(%s) failed for account=%s",
            field,
            getattr(account, "pk", "?"),
        )


def _evaluate(rep, th) -> None:
    """Move ``rep.state`` between OK/WARNED/HALTED and alert on a new halt."""
    if rep.sent < th["min_volume"]:
        return

    prev = rep.state
    b, c = rep.bounce_rate, rep.complaint_rate

    if b >= th["bounce_halt"] or c >= th["complaint_halt"]:
        reason = (
            f"bounce rate {b:.2%} (halt {th['bounce_halt']:.2%})"
            if b >= th["bounce_halt"]
            else f"complaint rate {c:.2%} (halt {th['complaint_halt']:.2%})"
        )
        if prev != rep.State.HALTED:
            rep.state = rep.State.HALTED
            rep.state_changed_at = timezone.now()
            rep.halted_reason = reason
            rep.save(update_fields=["state", "state_changed_at", "halted_reason"])
            _alert_halt(rep, reason)
        return

    if b >= th["bounce_warn"]:
        if prev == rep.State.OK:
            rep.state = rep.State.WARNED
            rep.state_changed_at = timezone.now()
            rep.save(update_fields=["state", "state_changed_at"])
            logger.warning(
                "Account %s reputation WARNED: bounce rate %.2f%% over %d sends",
                rep.account_id,
                b * 100,
                rep.sent,
            )


def _alert_halt(rep, reason: str) -> None:
    logger.error(
        "REPUTATION HALT: account=%s %s (sent=%d bounced=%d complained=%d)",
        rep.account_id,
        reason,
        rep.sent,
        rep.bounced,
        rep.complained,
    )
    try:
        from apps.billing.slack import post_message

        post_message(
            f":rotating_light: Sender reputation halt — account {rep.account_id} "
            f"({rep.account}). {reason}. Non-system sends are blocked until reset in "
            f"the Mail provider settings screen."
        )
    except Exception:
        logger.exception("reputation halt Slack alert failed")


def check_can_send(account, *, system: bool = False) -> tuple[bool, str]:
    """Return ``(allowed, reason)``. System mail is always allowed."""
    if system or account is None:
        return True, ""
    try:
        from apps.email.models import SendReputation

        rep = SendReputation.objects.filter(account=account).first()
        if rep is not None and rep.state == rep.State.HALTED:
            return False, rep.halted_reason or "sender reputation halt"
    except Exception:
        logger.exception(
            "check_can_send failed for account=%s; allowing send",
            getattr(account, "pk", "?"),
        )
    return True, ""


def reset(account) -> None:
    """Operator action: clear a halt/warn and start a fresh window."""
    from apps.email.models import SendReputation

    SendReputation.objects.filter(account=account).update(
        state=SendReputation.State.OK,
        halted_reason="",
        state_changed_at=timezone.now(),
        window_started_at=timezone.now(),
        sent=0,
        bounced=0,
        complained=0,
    )
    logger.info("Reputation manually reset for account=%s", getattr(account, "pk", "?"))
