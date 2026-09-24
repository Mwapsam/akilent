"""Centralized email suppression list management.

Replaces ad hoc SuppressionListEntry queries scattered across send.py, api/services.py,
and tasks.py with a single source of truth. Handles:
- Account-scoped and global suppression checks
- Upsert logic that updates bounce_count, reason, bounce_type on repeat events
- Auto-escalation of soft bounces to hard suppression at configured threshold
- Recording which message triggered a suppression for auditing
"""

import logging
from typing import Optional

from django.db import transaction

from apps.email.models import GlobalSuppression, SuppressionListEntry, EmailMessage

logger = logging.getLogger(__name__)


def is_suppressed(account, email: str) -> bool:
    """Check if email is suppressed for a specific account.

    Only counts "blocking" reasons (BOUNCE, COMPLAINT, UNSUBSCRIBE, MANUAL, INVALID).
    SOFT_BOUNCE alone is not blocking.
    """
    blocking_reasons = [
        SuppressionListEntry.Reason.BOUNCE,
        SuppressionListEntry.Reason.COMPLAINT,
        SuppressionListEntry.Reason.UNSUBSCRIBE,
        SuppressionListEntry.Reason.MANUAL,
        SuppressionListEntry.Reason.INVALID,
    ]
    if SuppressionListEntry.objects.filter(
        account=account,
        email=email,
        reason__in=blocking_reasons,
    ).exists():
        return True
    # A hard bounce or complaint for any tenant blocks the address for all of
    # them — AWS meters those rates against our whole SES account.
    return GlobalSuppression.objects.filter(email=email).exists()


def is_suppressed_globally(email: str) -> bool:
    """Check if email is suppressed across any account (global check).

    Used for system emails (password resets, etc.) that have no account context.
    Only counts "blocking" reasons.
    """
    blocking_reasons = [
        SuppressionListEntry.Reason.BOUNCE,
        SuppressionListEntry.Reason.COMPLAINT,
        SuppressionListEntry.Reason.UNSUBSCRIBE,
        SuppressionListEntry.Reason.MANUAL,
        SuppressionListEntry.Reason.INVALID,
    ]
    if SuppressionListEntry.objects.filter(
        email=email,
        reason__in=blocking_reasons,
    ).exists():
        return True
    return GlobalSuppression.objects.filter(email=email).exists()


def get_suppressed_emails(account, emails: list[str]) -> set[str]:
    """Get the set of suppressed email addresses from a list (account-scoped).

    Used for bulk campaign dispatch to filter out suppressed recipients efficiently.
    """
    blocking_reasons = [
        SuppressionListEntry.Reason.BOUNCE,
        SuppressionListEntry.Reason.COMPLAINT,
        SuppressionListEntry.Reason.UNSUBSCRIBE,
        SuppressionListEntry.Reason.MANUAL,
        SuppressionListEntry.Reason.INVALID,
    ]
    suppressed = set(
        SuppressionListEntry.objects.filter(
            account=account,
            email__in=emails,
            reason__in=blocking_reasons,
        ).values_list("email", flat=True)
    )
    # Second query, not a join: this is the hot bulk-dispatch path.
    suppressed |= set(
        GlobalSuppression.objects.filter(email__in=emails).values_list(
            "email", flat=True
        )
    )
    return suppressed


def record_global_event(
    email: str,
    reason: str,
    bounce_type: str = "",
    source: str = "ses_webhook",
) -> GlobalSuppression:
    """Suppress an address platform-wide, across every tenant.

    Upserts: a repeat event bumps ``hit_count`` and refreshes ``last_seen``
    rather than creating a second row. Only hard failures belong here —
    unsubscribes are a per-sender consent withdrawal and stay account-scoped.
    """
    with transaction.atomic():
        entry, created = GlobalSuppression.objects.select_for_update().get_or_create(
            email=email,
            defaults={
                "reason": reason,
                "bounce_type": bounce_type,
                "source": source,
            },
        )
        if not created:
            entry.hit_count += 1
            entry.reason = reason
            if bounce_type:
                entry.bounce_type = bounce_type
            if source:
                entry.source = source
            entry.save()
        else:
            logger.info(
                "Platform-wide suppression for %s (reason=%s, source=%s)",
                email,
                reason,
                source,
            )
    return entry


def record_event(
    account,
    email: str,
    reason: str,
    bounce_type: str = "",
    message: Optional[EmailMessage] = None,
) -> SuppressionListEntry:
    """Record a suppression event (bounce, complaint, unsubscribe, validation failure, etc).

    Upserts the suppression row: if it already exists, bumps bounce_count and updates
    reason/bounce_type/triggered_by_message/updated_at. For SOFT_BOUNCE reasons, if the
    existing row is already SOFT_BOUNCE and the new count reaches the configured threshold,
    automatically escalates the row to BOUNCE (permanent suppression).

    Args:
        account: The Account instance
        email: Email address being suppressed
        reason: One of SuppressionListEntry.Reason choices
        bounce_type: SES bounce type (Permanent, Transient, Undetermined), if applicable
        message: Optional EmailMessage that triggered this event (for auditing)

    Returns:
        The created or updated SuppressionListEntry row
    """
    from apps.core.models import MailProviderSettings

    settings = MailProviderSettings.load()
    soft_bounce_threshold = settings.soft_bounce_threshold

    with transaction.atomic():
        entry, created = SuppressionListEntry.objects.select_for_update().get_or_create(
            account=account,
            email=email,
            defaults={
                "reason": reason,
                "bounce_type": bounce_type,
                "bounce_count": 1,
                "triggered_by_message": message,
            },
        )

        if not created:
            # Update existing entry
            entry.bounce_count += 1
            entry.reason = reason
            if bounce_type:
                entry.bounce_type = bounce_type
            if message:
                entry.triggered_by_message = message

            # Check for soft-bounce escalation
            if (
                reason == SuppressionListEntry.Reason.SOFT_BOUNCE
                and entry.bounce_count >= soft_bounce_threshold
            ):
                logger.info(
                    "Auto-escalating soft bounces for %s (account=%s, count=%s)",
                    email,
                    account.id,
                    entry.bounce_count,
                )
                entry.reason = SuppressionListEntry.Reason.BOUNCE

            entry.save()

    # Hard failures are a property of the address, not of the tenant that hit
    # it: mirror them into the platform-wide list so no other account mails it.
    if reason in (
        SuppressionListEntry.Reason.BOUNCE,
        SuppressionListEntry.Reason.COMPLAINT,
    ):
        record_global_event(
            email, reason, bounce_type=bounce_type, source="ses_webhook"
        )

    return entry
