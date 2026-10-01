"""Staged contact import pipeline.

The pipeline separates raw data ingestion from contact creation, ensuring
contacts only enter the database after validation and deduplication.

Stages
------
1. parse      — read rows from a dict iterable (CSV, JSON, etc.)
2. validate   — normalize phone via normalize_phone(); validate email via validate_recipient()
3. deduplicate — check against existing contacts in the account
4. score      — compute initial quality score
5. create     — create Contact + ContactPhone + ContactEmail rows

Usage
-----
    from apps.contacts.importer import import_rows

    result = import_rows(account, rows)
    # result.total, result.valid_phones, result.valid_emails,
    # result.duplicates, result.created, result.errors
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from apps.contacts.models import Contact, ContactEmail, ContactPhone
from apps.contacts.quality import update_quality_score

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    total: int = 0
    valid_phones: int = 0
    valid_emails: int = 0
    duplicates: int = 0
    created: int = 0
    errors: list[dict] = field(default_factory=list)

    @property
    def invalid_phones(self) -> int:
        return self.total - self.valid_phones - self.duplicates

    def summary(self) -> dict:
        return {
            "total": self.total,
            "valid_phones": self.valid_phones,
            "valid_emails": self.valid_emails,
            "duplicates": self.duplicates,
            "created": self.created,
            "errors": len(self.errors),
        }


def import_rows(account, rows: list[dict]) -> ImportResult:
    """Import a list of contact dicts into *account*.

    Each row should have at least one of: phone, email.
    Optional keys: first_name, last_name, attributes (dict).
    """
    result = ImportResult()

    for i, raw in enumerate(rows):
        result.total += 1
        try:
            _process_row(account, raw, result)
        except Exception as exc:
            logger.warning("import_rows: row %d failed: %s", i, exc)
            result.errors.append({"row": i, "error": str(exc), "raw": raw})

    return result


def _process_row(account, raw: dict, result: ImportResult) -> None:
    raw_phone: str = (raw.get("phone") or "").strip()
    raw_email: str = (raw.get("email") or "").strip().lower()
    first_name: str = (raw.get("first_name") or "").strip()
    last_name: str = (raw.get("last_name") or "").strip()
    attributes: dict = raw.get("attributes") or {}

    if not raw_phone and not raw_email:
        result.errors.append({"error": "row has neither phone nor email", "raw": raw})
        return

    # Stage 2: validate
    phone_result = _validate_phone(raw_phone) if raw_phone else None
    email_result = _validate_email(raw_email) if raw_email else None

    if phone_result and phone_result["is_valid_format"]:
        result.valid_phones += 1
    if email_result and email_result["is_valid_format"]:
        result.valid_emails += 1

    # Stage 3: deduplicate
    normalized_phone = phone_result["normalized_value"] if phone_result else None
    existing = _find_existing(account, normalized_phone, raw_email)
    if existing:
        result.duplicates += 1
        return

    # Stage 4 + 5: create
    _create_contact(
        account,
        phone_result=phone_result,
        email_result=email_result,
        first_name=first_name,
        last_name=last_name,
        attributes=attributes,
        result=result,
    )


def _validate_phone(raw: str) -> dict:
    from apps.whatsapp.models.contact import normalize_phone

    try:
        import phonenumbers

        parsed = phonenumbers.parse(raw, "ZM")
        is_valid = phonenumbers.is_valid_number(parsed)
        normalized = phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.E164
        )
        country = phonenumbers.region_code_for_number(parsed) or ""
    except Exception:
        try:
            normalized = normalize_phone(raw)
            is_valid = True
            country = ""
        except Exception:
            normalized = raw
            is_valid = False
            country = ""

    return {
        "raw_value": raw,
        "normalized_value": normalized,
        "country": country,
        "is_valid_format": is_valid,
    }


def _validate_email(email: str) -> dict:
    from apps.email.services.validation import has_mx_record, is_valid_syntax

    is_valid_fmt = is_valid_syntax(email)
    domain_valid = False
    deliverability = "unknown"

    if is_valid_fmt:
        try:
            domain = email.split("@", 1)[1]
            domain_valid = has_mx_record(domain)
            deliverability = "valid" if domain_valid else "invalid"
        except Exception:
            pass

    return {
        "email": email,
        "is_valid_format": is_valid_fmt,
        "domain_valid": domain_valid,
        "deliverability_status": deliverability,
        "last_checked": timezone.now(),
    }


def _find_existing(account, normalized_phone: str | None, email: str | None):
    if normalized_phone:
        existing = Contact.objects.filter(
            account=account, phone=normalized_phone
        ).first()
        if existing:
            return existing
    if email:
        existing = Contact.objects.filter(account=account, email=email).first()
        if existing:
            return existing
    return None


@transaction.atomic
def _create_contact(
    account,
    *,
    phone_result: dict | None,
    email_result: dict | None,
    first_name: str,
    last_name: str,
    attributes: dict,
    result: ImportResult,
) -> None:
    normalized_phone = phone_result["normalized_value"] if phone_result else None
    email = email_result["email"] if email_result else None

    contact = Contact.objects.create(
        account=account,
        phone=normalized_phone,
        email=email,
        first_name=first_name,
        last_name=last_name,
        attributes=attributes,
        lifecycle_stage=Contact.LifecycleStage.IMPORTED,
        source="import",
    )

    if phone_result:
        ContactPhone.objects.create(
            contact=contact,
            raw_value=phone_result["raw_value"],
            normalized_value=phone_result["normalized_value"],
            country=phone_result["country"],
            is_valid_format=phone_result["is_valid_format"],
            is_primary=True,
        )

    if email_result:
        ContactEmail.objects.create(
            contact=contact,
            email=email_result["email"],
            is_valid_format=email_result["is_valid_format"],
            domain_valid=email_result["domain_valid"],
            deliverability_status=email_result["deliverability_status"],
            last_checked=email_result["last_checked"],
            is_primary=True,
        )

    # Stage 4: score (after sub-models exist so the quality function can read them)
    update_quality_score(contact, account)
    result.created += 1
