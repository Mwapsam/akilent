"""Who may receive Akilent's marketing email, and the rules for changing that.

Rules:

* A person's own choice (signup checkbox, profile toggle) always wins, in both directions.
* An import or an operator never re-subscribes someone who unsubscribed or whose address
  bounced/complained. It may only add new addresses or update details of subscribed ones.
* Unsubscribing here touches only marketing. System mail is governed by
  apps.email.services.suppression.is_suppressed_globally, which this never writes to.
"""

from __future__ import annotations

from django.utils import timezone

from apps.platform_mail.models import MarketingSubscriber

Status = MarketingSubscriber.Status
Source = MarketingSubscriber.Source

_PERSONAL = (Source.SIGNUP, Source.PROFILE)


def normalise(email: str) -> str:
    return (email or "").strip().lower()


def subscribe(
    email: str,
    *,
    source: str,
    name: str = "",
    user=None,
    evidence: dict | None = None,
    consented_at=None,
) -> tuple[MarketingSubscriber, str]:
    """Record consent for ``email``. Returns (subscriber, outcome).

    outcome is "created", "resubscribed", "updated" or "kept_opted_out" (an import or
    operator met an address that said no; nothing changed).
    """
    email = normalise(email)
    now = timezone.now()
    row = MarketingSubscriber.objects.filter(email=email).first()
    if row is None:
        row = MarketingSubscriber.objects.create(
            email=email,
            name=name[:200],
            user=user,
            source=source,
            status=Status.SUBSCRIBED,
            consent_evidence=evidence or {},
            consented_at=consented_at or now,
        )
        return row, "created"

    changed = []
    if user is not None and row.user_id is None:
        row.user = user
        changed.append("user")
    if name and not row.name:
        row.name = name[:200]
        changed.append("name")

    if not row.is_subscribed:
        if source not in _PERSONAL:
            if changed:
                row.save(update_fields=[*changed, "updated_at"])
            return row, "kept_opted_out"
        row.status = Status.SUBSCRIBED
        row.source = source
        row.consent_evidence = evidence or {}
        row.consented_at = consented_at or now
        row.unsubscribed_at = None
        row.save()
        return row, "resubscribed"

    if changed:
        row.save(update_fields=[*changed, "updated_at"])
    return row, "updated"


def unsubscribe(row: MarketingSubscriber, *, how: str) -> bool:
    """Stop marketing to ``row``. Returns False if it was already not subscribed."""
    if not row.is_subscribed:
        return False
    row.status = Status.UNSUBSCRIBED
    row.unsubscribed_at = timezone.now()
    evidence = dict(row.consent_evidence or {})
    evidence["unsubscribed_via"] = how
    row.consent_evidence = evidence
    row.save(
        update_fields=["status", "unsubscribed_at", "consent_evidence", "updated_at"]
    )
    return True


def mark_suppressed(row: MarketingSubscriber) -> None:
    """The address hard-bounced or complained (it is on the global suppression list)."""
    if row.status != Status.SUPPRESSED:
        row.status = Status.SUPPRESSED
        row.save(update_fields=["status", "updated_at"])


def user_wants_updates(user) -> bool:
    """Whether the user's *current* address is subscribed."""
    row = _current_row(user)
    return row is not None and row.is_subscribed


def set_user_preference(user, wants: bool, *, source: str, request=None) -> None:
    """The person's own choice from signup or profile settings."""
    if not user.email:
        return
    sync_user_email(user)
    if wants:
        evidence = {"form": source, "at": timezone.now().isoformat()}
        if request is not None:
            evidence["ip"] = request.META.get("REMOTE_ADDR", "")
        subscribe(
            user.email,
            source=source,
            name=user.get_full_name(),
            user=user,
            evidence=evidence,
        )
    else:
        # Every row tied to this person, not just one: an address they used before must
        # not keep getting marketing after they said no.
        for row in _rows_for_user(user):
            unsubscribe(row, how=source)


def sync_user_email(user) -> None:
    """Keep one consent row per person after they change their email address.

    The row linked to an old address moves to the new one, consent and all, when the new
    address has no row of its own. If it already has one, that row wins and the old rows
    are unsubscribed. Either way no stale address keeps getting marketing.
    """
    email = normalise(user.email)
    if not email:
        return
    current = MarketingSubscriber.objects.filter(email=email).first()
    stale = list(MarketingSubscriber.objects.filter(user=user).exclude(email=email))
    if current is None and stale:
        keep, stale = stale[0], stale[1:]
        keep.email = email
        keep.save(update_fields=["email", "updated_at"])
        current = keep
    if current is not None and current.user_id is None:
        current.user = user
        current.save(update_fields=["user", "updated_at"])
    for row in stale:
        unsubscribe(row, how="email_changed")
        row.user = None
        row.save(update_fields=["user", "updated_at"])


def _current_row(user):
    if not user.email:
        return None
    return MarketingSubscriber.objects.filter(email=normalise(user.email)).first()


def _rows_for_user(user):
    rows = {r.pk: r for r in MarketingSubscriber.objects.filter(user=user)}
    current = _current_row(user)
    if current is not None:
        rows.setdefault(current.pk, current)
    return list(rows.values())
