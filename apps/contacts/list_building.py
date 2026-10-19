"""Putting people on a customer list — the audience a WhatsApp campaign sends to.

Every way of filling a list goes through here: pasted phone numbers, a CSV
upload, a selection from the Customers page, "everyone who messaged us". Each
path ends the same way, so whatever the owner used, the people on the list:

- exist as customers (created by phone if new, within the plan's customer limit);
- have a WhatsApp identity, so a campaign can reach them (not skipped as
  "no WhatsApp number");
- are opted in when the owner attested that they agreed to hear from the
  business — never overriding someone who opted out (STOP always wins).

Work is batched (one lookup per batch for existing customers, identities and
memberships), so a few thousand rows is a few seconds, not a few thousand
round trips each.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.contacts.models import Contact, ContactImport, ContactListMembership

logger = logging.getLogger(__name__)

MAX_PASTE_LINES = 1000
MAX_CSV_ROWS = 5000
MAX_CSV_BYTES = 2 * 1024 * 1024
_BATCH = 500

_PHONE_HEADERS = ("phone", "phone number", "mobile", "whatsapp", "number", "cell")
_NAME_HEADERS = ("name", "full name", "customer", "customer name")
_FIRST_HEADERS = ("first name", "first_name", "firstname")
_LAST_HEADERS = ("last name", "last_name", "lastname", "surname")
_TAG_HEADERS = ("tags", "tag", "labels")


class ListBuildError(ValueError):
    """The input can't be used at all (shown to the owner as-is)."""


@dataclass
class Row:
    line: int
    phone: str
    first_name: str = ""
    last_name: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class ListAddResult:
    added: int = 0  # newly put on this list
    already_on_list: int = 0
    created: int = 0  # brand-new customers
    opted_in: int = 0  # marked opted in by this attestation
    opted_out_unchanged: int = 0  # had opted out; left that way
    over_limit: int = 0  # not added: the plan's customer limit
    invalid: list[tuple[int, str, str]] = field(default_factory=list)  # line, raw, why

    @property
    def total_seen(self) -> int:
        return self.added + self.already_on_list + self.over_limit + len(self.invalid)

    def merge(self, other: ListAddResult) -> None:
        for name in (
            "added",
            "already_on_list",
            "created",
            "opted_in",
            "opted_out_unchanged",
            "over_limit",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.invalid.extend(other.invalid)


# --- Parsing -----------------------------------------------------------------


def _split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split(None, 1)
    if not parts:
        return "", ""
    return parts[0][:150], (parts[1] if len(parts) > 1 else "")[:150]


def parse_pasted(text: str) -> list[Row]:
    """One person per line: ``+260971234567`` or ``+260971234567, Ada Mwape``.

    A comma, tab or semicolon separates the number from the name; blank lines
    are ignored.
    """
    lines = (text or "").splitlines()
    rows: list[Row] = []
    for i, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        for sep in ("\t", ";", ","):
            if sep in raw:
                phone, name = raw.split(sep, 1)
                break
        else:
            phone, name = raw, ""
        first, last = _split_name(name)
        rows.append(Row(line=i, phone=phone.strip(), first_name=first, last_name=last))
    if len(rows) > MAX_PASTE_LINES:
        raise ListBuildError(
            f"That's {len(rows):,} numbers — paste up to {MAX_PASTE_LINES:,} at a time, "
            "or upload them as a CSV file."
        )
    if not rows:
        raise ListBuildError("Paste at least one phone number.")
    return rows


def _find_column(headers: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {h.strip().lower(): h for h in headers if h}
    for c in candidates:
        if c in lowered:
            return lowered[c]
    return None


def detect_columns(headers: list[str]) -> dict:
    """Which CSV column holds what — shown to the owner before importing."""
    return {
        "phone": _find_column(headers, _PHONE_HEADERS),
        "name": _find_column(headers, _NAME_HEADERS),
        "first_name": _find_column(headers, _FIRST_HEADERS),
        "last_name": _find_column(headers, _LAST_HEADERS),
        "tags": _find_column(headers, _TAG_HEADERS),
    }


def read_csv(data: bytes) -> tuple[list[str], list[dict]]:
    if len(data) > MAX_CSV_BYTES:
        raise ListBuildError(
            "That file is larger than 2 MB. Split it into smaller files."
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h]
    records = list(reader)
    if len(records) > MAX_CSV_ROWS:
        raise ListBuildError(
            f"That file has {len(records):,} rows — upload up to {MAX_CSV_ROWS:,} at a time."
        )
    return headers, records


def parse_csv(data: bytes) -> list[Row]:
    """Phone-first CSV: needs a phone column; name, first/last name, tags optional.

    Row numbers match what the owner sees in a spreadsheet (header is row 1).
    """
    headers, records = read_csv(data)
    cols = detect_columns(headers)
    if cols["phone"] is None:
        raise ListBuildError(
            "We couldn't find a phone number column. Name it “phone” (or “mobile” / "
            "“whatsapp”) and upload again."
        )
    rows: list[Row] = []
    for i, rec in enumerate(records, start=2):
        phone = (rec.get(cols["phone"]) or "").strip()
        if not phone and not any((v or "").strip() for v in rec.values()):
            continue  # blank spreadsheet row
        first = (
            (rec.get(cols["first_name"]) or "").strip() if cols["first_name"] else ""
        )
        last = (rec.get(cols["last_name"]) or "").strip() if cols["last_name"] else ""
        if not first and cols["name"]:
            first, last = _split_name(rec.get(cols["name"]) or "")
        tags = []
        if cols["tags"]:
            raw = rec.get(cols["tags"]) or ""
            tags = [t.strip() for t in raw.replace(";", ",").split(",") if t.strip()]
        rows.append(
            Row(
                line=i,
                phone=phone,
                first_name=first[:150],
                last_name=last[:150],
                tags=tags,
            )
        )
    if not rows:
        raise ListBuildError("That file has no rows to add.")
    return rows


# --- Adding ------------------------------------------------------------------


def attestation_source(user) -> str:
    return f"owner_attestation:user:{getattr(user, 'pk', '') or ''}"


def _ensure_identities(account, contacts: list[Contact]) -> dict:
    """WhatsApp identity per contact id, created/linked in bulk. Batch-sized input."""
    from apps.whatsapp.models import WhatsAppContact
    from apps.whatsapp.models.contact import normalize_phone
    from apps.whatsapp.services.contacts import ensure_whatsapp_identity

    by_phone: dict[str, Contact] = {}
    resolved: dict = {}
    for c in contacts:
        if not c.phone:
            continue
        try:
            is_clean = normalize_phone(c.phone) == c.phone
        except ValidationError:
            continue  # no usable number: nothing to message
        if is_clean:
            by_phone[c.phone] = c
        else:
            # Stored un-normalized (e.g. synced from another system): the one-off
            # helper normalizes before it creates anything.
            wc = ensure_whatsapp_identity(c)
            if wc is not None and wc.contact_id == c.pk:
                resolved[c.pk] = wc
    if not by_phone:
        return resolved
    existing = {
        wc.phone_number: wc
        for wc in WhatsAppContact.objects.filter(
            account=account, phone_number__in=list(by_phone)
        )
    }
    to_link = []
    for phone, wc in existing.items():
        if wc.contact_id is None:
            wc.contact = by_phone[phone]
            to_link.append(wc)
    if to_link:
        WhatsAppContact.objects.bulk_update(to_link, ["contact"])
    missing = [phone for phone in by_phone if phone not in existing]
    if missing:
        # Phones here are already E.164 (normalized on the way in), so skipping
        # WhatsAppContact.save()'s re-normalization in bulk_create is safe.
        WhatsAppContact.objects.bulk_create(
            [
                WhatsAppContact(account=account, phone_number=p, contact=by_phone[p])
                for p in missing
            ],
            ignore_conflicts=True,
        )
        for wc in WhatsAppContact.objects.filter(
            account=account, phone_number__in=missing
        ):
            existing[wc.phone_number] = wc
    resolved.update(
        {
            by_phone[p].pk: wc
            for p, wc in existing.items()
            if p in by_phone and wc.contact_id in (None, by_phone[p].pk)
        }
    )
    return resolved


def _apply_attestation(identities: dict, attested_by, result: ListAddResult) -> None:
    from apps.whatsapp.models import WhatsAppContact

    source = attestation_source(attested_by)
    for wc in identities.values():
        if wc.opt_in_status == WhatsAppContact.OptInStatus.OPTED_OUT:
            result.opted_out_unchanged += 1
        elif wc.opt_in_status != WhatsAppContact.OptInStatus.OPTED_IN:
            wc.record_opt_in(source)
            result.opted_in += 1


def _add_memberships(
    contact_list, contacts: list[Contact], result: ListAddResult
) -> None:
    ids = {c.pk for c in contacts}
    if contact_list is None:  # adding customers without putting them on a list
        result.added += len(ids)
        return
    already = set(
        ContactListMembership.objects.filter(
            contact_list=contact_list, contact_id__in=ids
        ).values_list("contact_id", flat=True)
    )
    new = ids - already
    ContactListMembership.objects.bulk_create(
        [
            ContactListMembership(contact_list=contact_list, contact_id=cid)
            for cid in new
        ],
        ignore_conflicts=True,
    )
    result.added += len(new)
    result.already_on_list += len(already)


def add_people_to_list(
    account,
    contact_list,
    rows: list[Row],
    *,
    attested_by=None,
    source: str = "list",
    filename: str = "",
) -> ListAddResult:
    """Add phone-number rows to *contact_list*, creating customers as needed."""
    from apps.billing import api as billing_api
    from apps.contacts.api import count_contacts
    from apps.contacts.services import upsert_contact_by_phone
    from apps.contacts.tags import TagError, add_tag
    from apps.whatsapp.models.contact import normalize_phone

    result = ListAddResult()

    # Normalize first, so the same person typed two ways is one person.
    clean: dict[str, Row] = {}
    for row in rows:
        if not row.phone:
            result.invalid.append((row.line, "", "No phone number"))
            continue
        try:
            phone = normalize_phone(row.phone)
        except ValidationError:
            result.invalid.append(
                (row.line, row.phone, "Not a valid number — include the country code")
            )
            continue
        if phone in clean:
            continue  # same number twice in the input
        row.phone = phone
        clean[phone] = row

    allowed = billing_api.limit(account, "contacts")
    total = count_contacts(account)
    phones = list(clean)
    for start in range(0, len(phones), _BATCH):
        batch = phones[start : start + _BATCH]
        with transaction.atomic():
            existing = {
                c.phone: c
                for c in Contact.objects.filter(account=account, phone__in=batch)
            }
            contacts: list[Contact] = []
            for phone in batch:
                row = clean[phone]
                contact = existing.get(phone)
                if contact is None:
                    if 0 <= allowed <= total:
                        result.over_limit += 1
                        continue
                    contact, created = upsert_contact_by_phone(
                        account,
                        phone,
                        source=source,
                        first_name=row.first_name,
                        last_name=row.last_name,
                    )
                    if created:
                        total += 1
                        result.created += 1
                else:
                    # Fill blanks only: a list upload never renames someone the
                    # owner already knows.
                    dirty = []
                    if row.first_name and not contact.first_name:
                        contact.first_name = row.first_name
                        dirty.append("first_name")
                    if row.last_name and not contact.last_name:
                        contact.last_name = row.last_name
                        dirty.append("last_name")
                    if dirty:
                        contact.save(update_fields=[*dirty, "updated_at"])
                for tag in row.tags:
                    try:
                        add_tag(contact, tag)
                    except TagError:
                        pass  # a bad or excess tag never blocks adding the person
                contacts.append(contact)

            identities = _ensure_identities(account, contacts)
            if attested_by is not None:
                _apply_attestation(identities, attested_by, result)
            _add_memberships(contact_list, contacts, result)

    ContactImport.objects.create(
        account=account,
        filename=(filename or f"{source}:list:{getattr(contact_list, 'pk', '')}")[:255],
        row_count=len(rows),
        created_count=result.created,
        updated_count=result.added + result.already_on_list - result.created,
        skipped_count=len(result.invalid),
        limit_skipped_count=result.over_limit,
        mapping={
            "list": getattr(contact_list, "pk", None),
            "source": source,
            "attested": attested_by is not None,
        },
    )
    return result


def add_contacts_to_list(
    account, contact_list, contacts_qs, *, attested_by=None
) -> ListAddResult:
    """Add existing customers (a selection, a filter, everyone who messaged us)."""
    result = ListAddResult()
    ids = list(contacts_qs.filter(account=account).values_list("pk", flat=True))
    for start in range(0, len(ids), _BATCH):
        batch = list(Contact.objects.filter(pk__in=ids[start : start + _BATCH]))
        with transaction.atomic():
            identities = _ensure_identities(account, batch)
            if attested_by is not None:
                _apply_attestation(identities, attested_by, result)
            _add_memberships(contact_list, batch, result)
    return result


def invalid_rows_csv(result: ListAddResult) -> str:
    """The rows that weren't added, as a CSV the owner can fix and re-upload."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["row", "phone", "problem"])
    for line, raw, why in result.invalid:
        # Neutralise spreadsheet formulas in echoed input.
        safe = (
            "'" + raw
            if raw[:1] in ("=", "+", "-", "@") and not raw[1:2].isdigit()
            else raw
        )
        writer.writerow([line, safe, why])
    return out.getvalue()
