"""List membership: dynamic filters over Akilent users, CSV imports, campaign recipients."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q, QuerySet

from apps.platform_mail.models import (
    MarketingList,
    MarketingListMember,
    MarketingSubscriber,
)
from apps.platform_mail.services import consent

MAX_IMPORT_ROWS = 50_000


def dynamic_members(filters: dict) -> QuerySet[MarketingSubscriber]:
    """Subscribed Akilent users matching ``filters`` (see MarketingList.filters)."""
    from apps.accounts.models import Membership

    memberships = Membership.objects.filter(user__is_active=True)
    if filters.get("role") == "owners":
        memberships = memberships.filter(role=Membership.Role.OWNER)
    if filters.get("plans"):
        memberships = memberships.filter(
            account__subscription__plan__slug__in=filters["plans"]
        )
    if filters.get("statuses"):
        memberships = memberships.filter(
            account__subscription__status__in=filters["statuses"]
        )
    if filters.get("joined_after"):
        memberships = memberships.filter(
            user__date_joined__date__gte=filters["joined_after"]
        )
    if filters.get("joined_before"):
        memberships = memberships.filter(
            user__date_joined__date__lte=filters["joined_before"]
        )
    user_ids = memberships.values("user_id")
    return MarketingSubscriber.objects.filter(
        user_id__in=user_ids, status=MarketingSubscriber.Status.SUBSCRIBED
    )


def members(mlist: MarketingList) -> QuerySet[MarketingSubscriber]:
    """Everyone on the list, whatever their status (the list page shows opted-out rows too)."""
    if mlist.kind == MarketingList.Kind.DYNAMIC:
        return dynamic_members(mlist.filters or {})
    return mlist.members.all()


def recipient_ids(lists) -> set[int]:
    """Subscribed subscribers across ``lists``, de-duplicated."""
    ids: set[int] = set()
    for mlist in lists:
        qs = members(mlist).filter(status=MarketingSubscriber.Status.SUBSCRIBED)
        ids.update(qs.values_list("pk", flat=True))
    return ids


def add_member(mlist: MarketingList, subscriber: MarketingSubscriber) -> bool:
    _, created = MarketingListMember.objects.get_or_create(
        list=mlist, subscriber=subscriber
    )
    return created


@dataclass
class ImportReport:
    added: int = 0  # new subscribers
    existing: int = 0  # already known and subscribed; added to the list
    kept_opted_out: int = 0  # said no before; not re-subscribed, not added
    invalid: list[tuple[int, str]] = field(default_factory=list)  # (row number, reason)

    @property
    def total(self) -> int:
        return self.added + self.existing + self.kept_opted_out + len(self.invalid)


def import_csv(
    mlist: MarketingList, raw: bytes, *, consent_note: str, actor=None
) -> ImportReport:
    """Add the rows of a CSV exported from any CRM to a static list.

    Columns (header row, any order, case-insensitive): ``email`` (required), ``name``,
    ``consent_source``, ``consent_date`` (YYYY-MM-DD). ``consent_note`` is the operator's
    statement of how these people agreed; it is stored on every row.
    """
    if mlist.kind != MarketingList.Kind.STATIC:
        raise ValueError("Only static lists can be imported into.")
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower(): h for h in (reader.fieldnames or [])}
    if "email" not in headers:
        raise ValueError('The file needs an "email" column in its first row.')

    def col(row, name):
        key = headers.get(name)
        return (row.get(key) or "").strip() if key else ""

    report = ImportReport()
    seen: set[str] = set()
    for number, row in enumerate(reader, start=2):
        if number - 1 > MAX_IMPORT_ROWS:
            report.invalid.append(
                (number, f"Stopped: more than {MAX_IMPORT_ROWS:,} rows")
            )
            break
        email = consent.normalise(col(row, "email"))
        if not email:
            report.invalid.append((number, "No email"))
            continue
        try:
            validate_email(email)
        except ValidationError:
            report.invalid.append((number, f"Not a valid email: {email[:80]}"))
            continue
        if email in seen:
            continue
        seen.add(email)

        consented_at = None
        raw_date = col(row, "consent_date")
        if raw_date:
            try:
                consented_at = date.fromisoformat(raw_date[:10])
            except ValueError:
                report.invalid.append(
                    (number, f"Consent date isn't YYYY-MM-DD: {raw_date[:20]}")
                )
                continue
        evidence = {
            "import_list": mlist.name,
            "operator_note": consent_note,
            "consent_source": col(row, "consent_source"),
            "consent_date": raw_date,
            "imported_by": getattr(actor, "email", ""),
        }
        subscriber, outcome = consent.subscribe(
            email,
            source=MarketingSubscriber.Source.IMPORT,
            name=col(row, "name"),
            evidence=evidence,
            consented_at=_as_datetime(consented_at),
        )
        if outcome == "kept_opted_out":
            report.kept_opted_out += 1
            continue
        add_member(mlist, subscriber)
        if outcome == "created":
            report.added += 1
        else:
            report.existing += 1
    return report


def search(
    qs: QuerySet[MarketingSubscriber], term: str
) -> QuerySet[MarketingSubscriber]:
    term = (term or "").strip()
    if not term:
        return qs
    return qs.filter(Q(email__icontains=term) | Q(name__icontains=term))


def _as_datetime(d):
    if d is None:
        return None
    from datetime import datetime, time

    from django.utils import timezone

    return timezone.make_aware(datetime.combine(d, time(12)))
