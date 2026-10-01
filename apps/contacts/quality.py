"""Contact quality scoring.

compute_quality() returns an integer 0–100 representing how useful and reachable
a contact is *for this specific account*.  The score reflects business usefulness,
not just data validity.  Weights are uniform across accounts today; the signature
accepts account so per-industry weighting can be added later without changing callers.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone


def compute_quality(contact, account) -> int:
    """Return a quality score 0–100 for *contact* in the context of *account*.

    Components
    ----------
    +20  valid phone format       (ContactPhone.is_valid_format)
    +20  WhatsApp confirmed        (ContactPhone.whatsapp_status == confirmed)
    +15  valid + deliverable email (ContactEmail, valid format + domain + deliverable)
    +20  engagement history        (Contact.last_engaged_at is not None)
    +15  is a customer             (has at least one linked Order)
    +10  recent activity           (last conversation or order in the past 90 days)
    ----
    100  maximum
    """
    score = 0

    # --- Phone signals (from ContactPhone sub-model) ---
    primary_phone = contact.contact_phones.filter(is_primary=True).first()
    if primary_phone:
        if primary_phone.is_valid_format:
            score += 20
        if primary_phone.whatsapp_status == "confirmed":
            score += 20
    elif contact.phone:
        # Legacy: phone stored directly on Contact but no ContactPhone row yet
        score += 10  # partial credit — format assumed valid if E.164

    # --- Email signals (from ContactEmail sub-model) ---
    primary_email = contact.contact_emails.filter(is_primary=True).first()
    if primary_email:
        if (
            primary_email.is_valid_format
            and primary_email.domain_valid
            and primary_email.deliverability_status == "valid"
        ):
            score += 15
        elif primary_email.is_valid_format:
            score += 5  # partial: format OK but not fully verified
    elif contact.email:
        score += 5  # legacy email, not yet validated through ContactEmail

    # --- Engagement ---
    if contact.last_engaged_at is not None:
        score += 20

    # --- Customer status (has an order) ---
    has_order = False
    try:
        has_order = contact.orders.exists()
    except Exception:
        pass
    if has_order:
        score += 15

    # --- Recent activity (90 days) ---
    cutoff = timezone.now() - timedelta(days=90)
    recently_active = False
    if contact.last_engaged_at and contact.last_engaged_at >= cutoff:
        recently_active = True
    if not recently_active and has_order:
        try:
            recently_active = contact.orders.filter(created_at__gte=cutoff).exists()
        except Exception:
            pass
    if recently_active:
        score += 10

    return min(score, 100)


def update_quality_score(contact, account) -> None:
    """Compute and persist the quality score + lifecycle stage on *contact*.

    Does a targeted save so it does not clobber concurrent field updates.
    """

    score = compute_quality(contact, account)
    stage = _derive_lifecycle_stage(contact, has_order=score >= 15)

    contact.quality_score = score
    contact.quality_updated_at = timezone.now()
    contact.lifecycle_stage = stage
    contact.save(
        update_fields=[
            "quality_score",
            "quality_updated_at",
            "lifecycle_stage",
            "updated_at",
        ]
    )


def _derive_lifecycle_stage(contact, *, has_order: bool) -> str:
    from apps.contacts.models import Contact

    cutoff_90 = timezone.now() - timedelta(days=90)
    cutoff_180 = timezone.now() - timedelta(days=180)

    if has_order:
        if contact.last_engaged_at and contact.last_engaged_at >= cutoff_90:
            # Check if they've ordered more than once (repeat customer)
            try:
                order_count = contact.orders.count()
            except Exception:
                order_count = 1
            if order_count > 1:
                return Contact.LifecycleStage.REPEAT_CUSTOMER
            return Contact.LifecycleStage.CUSTOMER
        # Has orders but hasn't been active in 180+ days
        if contact.last_engaged_at and contact.last_engaged_at < cutoff_180:
            return Contact.LifecycleStage.INACTIVE
        return Contact.LifecycleStage.CUSTOMER

    if contact.last_engaged_at:
        if contact.last_engaged_at < cutoff_180:
            return Contact.LifecycleStage.INACTIVE
        return Contact.LifecycleStage.ENGAGED

    # Has valid phone or email -> validated
    primary_phone = contact.contact_phones.filter(
        is_primary=True, is_valid_format=True
    ).first()
    if primary_phone or (contact.phone and contact.email):
        return Contact.LifecycleStage.VALIDATED

    if contact.phone or contact.email:
        return Contact.LifecycleStage.IMPORTED

    return Contact.LifecycleStage.UNKNOWN
