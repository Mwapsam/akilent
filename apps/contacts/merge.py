"""Merge two customer records into one.

The same person often ends up twice: once from WhatsApp, once from Instagram,
once typed in at the till. ``merge_contacts`` folds the duplicate into the
primary in one transaction and deletes the duplicate.

Everything that points at a customer is moved, found from the model graph
(``Contact._meta``) rather than a hand-kept list, so a relation added later is
moved too. Where moving a row would break a one-per-customer rule, the rule
decides:

- an open lead on both → the duplicate's lead is closed as lost ("merged");
- an active workflow run on both for the same workflow → the duplicate's run
  is cancelled;
- the same campaign / list / HubSpot link on both → the duplicate's row is
  dropped (the primary already has it).

Anything else that can't be moved stops the whole merge, so nothing is ever
half-merged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.apps import apps
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.contacts.models import Contact, ContactEvent

logger = logging.getLogger(__name__)

# The duplicate's row is redundant when the primary already has the same one.
_REDUNDANT_ON_CONFLICT = {
    "whatsapp.WhatsAppCampaignRecipient",
    "contacts.ContactListMembership",
    "hubspot.HubSpotContactLink",
}
# Filled from the duplicate only when the primary's is empty.
_IDENTITY_FIELDS = ("first_name", "last_name", "locale", "source")


class MergeError(ValueError):
    """The merge can't be done as asked; nothing was changed."""


@dataclass
class MergeResult:
    primary: Contact
    moved: dict = field(default_factory=dict)  # model label -> rows moved
    dropped: dict = field(default_factory=dict)  # model label -> redundant rows dropped
    attribute_conflicts: dict = field(default_factory=dict)
    closed_leads: int = 0
    cancelled_runs: int = 0


def _fill_identity(primary: Contact, duplicate: Contact, result: MergeResult) -> list:
    changed = []
    for name in _IDENTITY_FIELDS:
        if not getattr(primary, name) and getattr(duplicate, name):
            setattr(primary, name, getattr(duplicate, name))
            changed.append(name)
    # Email and phone are unique per business: free them on the duplicate first.
    moved_unique = {}
    for name in ("email", "phone"):
        if not getattr(primary, name) and getattr(duplicate, name):
            moved_unique[name] = getattr(duplicate, name)
    if moved_unique:
        Contact.objects.filter(pk=duplicate.pk).update(**dict.fromkeys(moved_unique))
        for name, value in moved_unique.items():
            setattr(primary, name, value)
            changed.append(name)
    if duplicate.first_seen and (
        not primary.first_seen or duplicate.first_seen < primary.first_seen
    ):
        primary.first_seen = duplicate.first_seen
        changed.append("first_seen")
    if duplicate.last_engaged_at and (
        not primary.last_engaged_at
        or duplicate.last_engaged_at > primary.last_engaged_at
    ):
        primary.last_engaged_at = duplicate.last_engaged_at
        changed.append("last_engaged_at")
    attrs = dict(duplicate.attributes or {})
    for key, value in (primary.attributes or {}).items():
        if key in attrs and attrs[key] not in (None, "") and attrs[key] != value:
            result.attribute_conflicts[key] = {"kept": value, "dropped": attrs[key]}
        attrs[key] = value
    if attrs != (primary.attributes or {}):
        primary.attributes = attrs
        changed.append("attributes")
    return changed


def _resolve_known_conflicts(primary: Contact, duplicate: Contact, result) -> None:
    Lead = apps.get_model("crm", "Lead")
    open_statuses = [
        Lead.Status.NEW,
        Lead.Status.CONTACTED,
        Lead.Status.QUALIFIED,
    ]
    if Lead.objects.filter(contact=primary, status__in=open_statuses).exists():
        result.closed_leads = Lead.objects.filter(
            contact=duplicate, status__in=open_statuses
        ).update(status=Lead.Status.LOST)

    Run = apps.get_model("automation", "WorkflowRun")
    live = [Run.Status.ACTIVE, Run.Status.WAITING]
    primary_runs = set(
        Run.objects.filter(contact=primary, status__in=live).values_list(
            "workflow_id", "subject_key"
        )
    )
    if primary_runs:
        clash = Q()
        for workflow_id, subject_key in primary_runs:
            clash |= Q(workflow_id=workflow_id, subject_key=subject_key)
        result.cancelled_runs = (
            Run.objects.filter(contact=duplicate, status__in=live)
            .filter(clash)
            .update(status=Run.Status.CANCELLED)
        )


def _relations():
    """Every model row that points at a Contact: (model, field name)."""
    for rel in Contact._meta.get_fields():
        if (
            rel.auto_created
            and not rel.concrete
            and (rel.one_to_many or rel.one_to_one)
        ):
            yield rel.related_model, rel.field.name  # type: ignore[union-attr]


def _move(model, fname: str, primary: Contact, duplicate: Contact, result) -> None:
    label = model._meta.label
    qs = model._default_manager.filter(**{fname: duplicate})
    if not qs.exists():
        return
    try:
        with transaction.atomic():
            n = qs.update(**{fname: primary})
        result.moved[label] = n
        return
    except IntegrityError:
        pass
    moved = dropped = 0
    for pk in list(qs.values_list("pk", flat=True)):
        try:
            with transaction.atomic():
                model._default_manager.filter(pk=pk).update(**{fname: primary})
            moved += 1
        except IntegrityError as exc:
            if label not in _REDUNDANT_ON_CONFLICT:
                raise MergeError(
                    f"Couldn't move this customer's {model._meta.verbose_name}: it "
                    "clashes with the other customer's. Nothing was changed."
                ) from exc
            model._default_manager.filter(pk=pk).delete()
            dropped += 1
    if moved:
        result.moved[label] = moved
    if dropped:
        result.dropped[label] = dropped


def merge_contacts(primary: Contact, duplicate: Contact, *, actor=None) -> MergeResult:
    """Fold ``duplicate`` into ``primary`` and delete ``duplicate``."""
    if primary.pk == duplicate.pk:
        raise MergeError("Choose two different customers.")
    if primary.account_id != duplicate.account_id:
        raise MergeError("Those customers belong to different businesses.")

    with transaction.atomic():
        locked = {
            c.pk: c
            for c in Contact.objects.select_for_update().filter(
                pk__in=[primary.pk, duplicate.pk]
            )
        }
        if len(locked) != 2:
            raise MergeError("One of those customers no longer exists.")
        primary, duplicate = locked[primary.pk], locked[duplicate.pk]
        result = MergeResult(primary=primary)

        changed = _fill_identity(primary, duplicate, result)
        if changed:
            primary.save(update_fields=[*changed, "updated_at"])
        _resolve_known_conflicts(primary, duplicate, result)
        for model, fname in _relations():
            _move(model, fname, primary, duplicate, result)
        primary.tags.add(*duplicate.tags.all())

        ContactEvent.objects.create(
            account_id=primary.account_id,
            contact=primary,
            type="contact.merged",
            occurred_at=timezone.now(),
            data={
                "merged_public_id": duplicate.public_id,
                "merged_name": duplicate.full_name,
                "attribute_conflicts": result.attribute_conflicts,
                "by": getattr(actor, "pk", None),
            },
        )
        duplicate.delete()

    from apps.core.audit import platform_record

    platform_record(
        action="contact.merge",
        account=primary.account,
        actor=actor,
        resource_type="contact",
        resource_id=primary.public_id,
        metadata={"merged": duplicate.public_id, "moved": result.moved},
    )
    return result


# --- Duplicate suggestions ----------------------------------------------------


def _norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def suggested_duplicates(contact: Contact, limit: int = 5) -> list[Contact]:
    """Customers who look like the same person as ``contact``.

    Same email (ignoring case), same phone, or the same first and last name —
    typically one person who wrote on both WhatsApp and Instagram. Pairs the
    owner said are different people are left out.
    """
    base = Contact.objects.filter(account_id=contact.account_id).exclude(pk=contact.pk)
    q = Q(pk__in=[])
    if contact.email:
        q |= Q(email__iexact=_norm_email(contact.email))
    if contact.phone:
        q |= Q(phone=contact.phone)
    if contact.first_name and contact.last_name:
        q |= Q(
            first_name__iexact=contact.first_name,
            last_name__iexact=contact.last_name,
        )
    dismissed = set((contact.attributes or {}).get("_not_duplicates", []))
    return [
        c
        for c in base.filter(q)[: limit + len(dismissed)]
        if c.public_id not in dismissed
    ][:limit]


def dismiss_suggestion(contact: Contact, other: Contact) -> None:
    """Remember "these two are different people" on both, so it isn't suggested again."""
    for a, b in ((contact, other), (other, contact)):
        attrs = dict(a.attributes or {})
        seen = list(attrs.get("_not_duplicates", []))
        if b.public_id not in seen:
            seen.append(b.public_id)
        attrs["_not_duplicates"] = seen
        Contact.objects.filter(pk=a.pk).update(attributes=attrs)


def duplicate_groups(account, limit: int = 50) -> list[list[Contact]]:
    """Possible duplicates across the business: same email (any case) or same phone."""
    from django.db.models import Count
    from django.db.models.functions import Lower

    groups: list[list[Contact]] = []
    emails = (
        Contact.objects.filter(account=account, email__isnull=False)
        .annotate(e=Lower("email"))
        .values("e")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("e", flat=True)[:limit]
    )
    for e in emails:
        groups.append(list(Contact.objects.filter(account=account, email__iexact=e)))
    return groups
