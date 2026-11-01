"""Starting, scheduling, cancelling and delivering Akilent marketing campaigns.

Never checks the tenant Email channel: platform marketing must keep working while it is off.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.platform_mail.models import (
    MarketingCampaign,
    MarketingSend,
    MarketingSettings,
    MarketingSubscriber,
)
from apps.platform_mail.services import consent, lists, render

logger = logging.getLogger(__name__)

Status = MarketingCampaign.Status


class CampaignError(Exception):
    """Shown to the operator as is."""


def problems(campaign: MarketingCampaign) -> list[str]:
    """What stops this campaign being sent, in plain words. Empty means ready."""
    found = []
    if not MarketingSettings.load().postal_address.strip():
        found.append(
            "Add Akilent's postal address in marketing settings (required in every marketing email)."
        )
    if not campaign.subject.strip():
        found.append("Add a subject.")
    if not campaign.body.strip():
        found.append("Write the email body.")
    if not campaign.lists.exists():
        found.append("Choose at least one list.")
    if campaign.cta_url and not campaign.cta_label.strip():
        found.append("Give the button a label.")
    return found


def recipient_count(campaign: MarketingCampaign) -> int:
    return len(lists.recipient_ids(campaign.lists.all()))


def start(campaign: MarketingCampaign) -> int:
    """Lock in the recipients and queue the send. Returns how many will be emailed."""
    from apps.platform_mail.tasks import send_campaign

    with transaction.atomic():
        campaign = MarketingCampaign.objects.select_for_update().get(pk=campaign.pk)
        if campaign.status not in (Status.DRAFT, Status.SCHEDULED):
            raise CampaignError("This campaign has already been sent or cancelled.")
        issues = problems(campaign)
        if issues:
            raise CampaignError(" ".join(issues))
        ids = lists.recipient_ids(campaign.lists.all())
        if not ids:
            raise CampaignError("The chosen lists have no subscribed recipients.")
        MarketingSend.objects.bulk_create(
            [MarketingSend(campaign=campaign, subscriber_id=i) for i in ids],
            ignore_conflicts=True,
        )
        campaign.status = Status.SENDING
        campaign.started_at = timezone.now()
        campaign.save(update_fields=["status", "started_at", "updated_at"])
        transaction.on_commit(lambda: send_campaign.delay(campaign.pk))
    return len(ids)


def schedule(campaign: MarketingCampaign, when) -> None:
    if when <= timezone.now():
        raise CampaignError("Pick a time in the future.")
    issues = problems(campaign)
    if issues:
        raise CampaignError(" ".join(issues))
    # Conditional update, not a save of the object in hand: a campaign started or cancelled
    # meanwhile must not be pulled back into the schedule.
    changed = MarketingCampaign.objects.filter(
        pk=campaign.pk, status__in=(Status.DRAFT, Status.SCHEDULED)
    ).update(status=Status.SCHEDULED, scheduled_at=when, updated_at=timezone.now())
    if not changed:
        raise CampaignError("Only a draft can be scheduled.")


def unschedule(campaign: MarketingCampaign) -> bool:
    """Scheduled -> draft. Does nothing (returns False) if it has since started or ended."""
    return bool(
        MarketingCampaign.objects.filter(
            pk=campaign.pk, status=Status.SCHEDULED
        ).update(status=Status.DRAFT, scheduled_at=None, updated_at=timezone.now())
    )


def cancel(campaign: MarketingCampaign) -> int:
    """Stop a scheduled or sending campaign. Returns how many unsent emails were dropped."""
    with transaction.atomic():
        campaign = MarketingCampaign.objects.select_for_update().get(pk=campaign.pk)
        if campaign.status not in (Status.SCHEDULED, Status.SENDING):
            raise CampaignError(
                "Only a scheduled or sending campaign can be cancelled."
            )
        dropped = campaign.sends.filter(status=MarketingSend.Status.PENDING).update(
            status=MarketingSend.Status.SKIPPED, error="Campaign cancelled"
        )
        campaign.status = Status.CANCELLED
        campaign.finished_at = timezone.now()
        campaign.save(update_fields=["status", "finished_at", "updated_at"])
    return dropped


def send_test(campaign: MarketingCampaign, to_email: str) -> None:
    """Send this campaign to one address with a [Test] subject. Records nothing."""
    from apps.email.services.send import send_platform_marketing_email

    if not MarketingSettings.load().postal_address.strip():
        raise CampaignError("Add Akilent's postal address in marketing settings first.")
    prepared = render.prepare(campaign)
    subject, text, html, unsub = prepared.for_token(render.PREVIEW_TOKEN)
    send_platform_marketing_email(
        to_email,
        f"[Test] {subject}",
        text,
        html,
        from_name=prepared.from_name,
        unsubscribe_url=unsub,
    )


# A provider error leaves the send pending to be retried on a later run; only after this
# many tries is it given up as failed. Errors that hit every send (the provider is down,
# credentials expired) therefore pause the campaign instead of using up the whole list.
MAX_ATTEMPTS = 5

SENT, SKIPPED, FAILED, RETRY, EMPTY = "sent", "skipped", "failed", "retry", "empty"


def deliver_next(campaign_id: int, prepared: render.Prepared) -> str:
    """Claim and deliver one pending send. Returns what happened: SENT, SKIPPED, FAILED,
    RETRY (the provider refused; try again later) or EMPTY (nothing left to claim).

    One row per transaction: if the worker dies mid-batch, everything already sent stays
    marked sent and nobody gets the email twice.
    """
    from apps.email.services.send import send_platform_marketing_email
    from apps.email.services.suppression import is_suppressed_globally

    with transaction.atomic():
        send = (
            MarketingSend.objects.select_for_update(skip_locked=True)
            .select_related("subscriber")
            .filter(campaign_id=campaign_id, status=MarketingSend.Status.PENDING)
            .order_by("attempts", "pk")
            .first()
        )
        if send is None:
            return EMPTY
        subscriber = send.subscriber
        if not subscriber.is_subscribed:
            _finish(send, MarketingSend.Status.SKIPPED, "No longer subscribed")
            return SKIPPED
        if is_suppressed_globally(subscriber.email):
            consent.mark_suppressed(subscriber)
            _finish(send, MarketingSend.Status.SKIPPED, "Address bounced or complained")
            return SKIPPED
        subject, text, html, unsub = prepared.for_token(subscriber.token)
        try:
            send_platform_marketing_email(
                subscriber.email,
                subject,
                text,
                html,
                from_name=prepared.from_name,
                unsubscribe_url=unsub,
            )
        except Exception as exc:
            logger.exception("platform_mail: send %s failed", send.pk)
            error = str(exc)[:255]
            send.attempts += 1
            MarketingCampaign.objects.filter(pk=campaign_id).update(last_error=error)
            if send.attempts >= MAX_ATTEMPTS:
                send.error = error
                send.status = MarketingSend.Status.FAILED
                send.save(update_fields=["attempts", "error", "status"])
                return FAILED
            send.error = error
            send.save(update_fields=["attempts", "error"])
            return RETRY
        _finish(send, MarketingSend.Status.SENT, "")
    return SENT


def finish_if_done(campaign_id: int) -> bool:
    """Mark a sending campaign sent once nothing is pending. Returns True if it is done."""
    if MarketingSend.objects.filter(
        campaign_id=campaign_id, status=MarketingSend.Status.PENDING
    ).exists():
        return False
    MarketingCampaign.objects.filter(pk=campaign_id, status=Status.SENDING).update(
        status=Status.SENT, finished_at=timezone.now()
    )
    return True


def stats(campaign: MarketingCampaign) -> dict:
    from django.db.models import Count

    counts = dict(
        campaign.sends.values_list("status")
        .annotate(n=Count("pk"))
        .values_list("status", "n")
    )
    unsubscribed = campaign.sends.filter(
        subscriber__status=MarketingSubscriber.Status.UNSUBSCRIBED,
        subscriber__unsubscribed_at__gte=campaign.started_at or timezone.now(),
    ).count()
    return {
        "total": sum(counts.values()),
        "sent": counts.get("sent", 0),
        "pending": counts.get("pending", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
        "unsubscribed": unsubscribed,
    }


def _finish(send: MarketingSend, status: str, error: str) -> None:
    send.status = status
    send.error = error
    send.sent_at = timezone.now() if status == MarketingSend.Status.SENT else None
    send.save(update_fields=["status", "error", "sent_at"])
