import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from celery import shared_task

logger = logging.getLogger(__name__)

# How long a run may hold a campaign. Covers one batch plus the minute until the run it
# queues; a chain that dies lets it lapse and the backstop takes over.
LEASE = timedelta(minutes=10)
NEXT_RUN_SECONDS = 60
# After a provider error, wait this long before trying again.
BACKOFF_SECONDS = 300


def _claim(campaign_id: int, token: str | None) -> str | None:
    """Take the campaign's lease, or None if another live chain holds it.

    A run may take the lease when nobody holds it, when it has lapsed, or when it was
    handed the current token by the run that queued it.
    """
    from apps.platform_mail.models import MarketingCampaign

    now = timezone.now()
    free = Q(lease_until__isnull=True) | Q(lease_until__lt=now)
    if token:
        free |= Q(lease_token=token)
    new_token = secrets.token_hex(16)
    claimed = (
        MarketingCampaign.objects.filter(
            pk=campaign_id, status=MarketingCampaign.Status.SENDING
        )
        .filter(free)
        .update(lease_token=new_token, lease_until=now + LEASE)
    )
    return new_token if claimed else None


@shared_task(queue="celery")
def send_campaign(campaign_id: int, lease_token: str | None = None) -> int:
    """Deliver up to one minute's worth of a campaign, then queue the next minute.

    Exactly one chain runs a campaign at a time (see _claim), so the per-minute cap holds
    however many times this is queued. Returns how many sends were processed this run.
    """
    from apps.platform_mail.models import MarketingCampaign
    from apps.platform_mail.services import render, sending

    token = _claim(campaign_id, lease_token)
    if token is None:
        return 0  # another chain is running it, or it is no longer sending
    campaign = MarketingCampaign.objects.get(pk=campaign_id)
    prepared = render.prepare(campaign)
    rate = max(1, getattr(settings, "PLATFORM_MARKETING_RATE_PER_MINUTE", 120))
    done = 0
    outcome = sending.EMPTY
    while done < rate:
        outcome = sending.deliver_next(campaign_id, prepared)
        if outcome in (sending.EMPTY, sending.RETRY):
            break
        done += 1
        if outcome == sending.SENT and campaign.last_error:
            MarketingCampaign.objects.filter(pk=campaign_id).update(last_error="")
            campaign.last_error = ""

    if sending.finish_if_done(campaign_id):
        MarketingCampaign.objects.filter(pk=campaign_id).update(
            lease_token="", lease_until=None
        )
        return done
    delay = BACKOFF_SECONDS if outcome == sending.RETRY else NEXT_RUN_SECONDS
    # Keep the lease until after the next run is due, then hand it on.
    MarketingCampaign.objects.filter(pk=campaign_id, lease_token=token).update(
        lease_until=timezone.now() + timedelta(seconds=delay) + LEASE
    )
    send_campaign.apply_async((campaign_id, token), countdown=delay)
    return done


@shared_task(queue="celery")
def send_due_campaigns() -> int:
    """Start every scheduled campaign whose time has come. Runs every minute (beat)."""
    from apps.platform_mail.models import MarketingCampaign
    from apps.platform_mail.services import sending

    started = 0
    due = MarketingCampaign.objects.filter(
        status=MarketingCampaign.Status.SCHEDULED, scheduled_at__lte=timezone.now()
    )
    for campaign in due:
        try:
            sending.start(campaign)
            started += 1
        except sending.CampaignError as exc:
            # Nothing can be sent as things stand: back to draft so an operator sees why.
            # unschedule only moves a campaign that is *still* scheduled, so one an operator
            # started or cancelled meanwhile is left alone.
            if sending.unschedule(campaign):
                logger.warning(
                    "platform_mail: scheduled campaign %s not started: %s",
                    campaign.pk,
                    exc,
                )
    return started


@shared_task(queue="celery")
def resume_stalled_campaigns() -> int:
    """Backstop: queue a run for sending campaigns whose chain has died.

    Only campaigns whose lease has lapsed are picked, and even a queued run that races a
    live chain can't take the lease and exits, so the send rate is never multiplied.
    """
    from apps.platform_mail.models import MarketingCampaign, MarketingSend

    ids = list(
        MarketingCampaign.objects.filter(
            status=MarketingCampaign.Status.SENDING,
            sends__status=MarketingSend.Status.PENDING,
        )
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lt=timezone.now()))
        .distinct()
        .values_list("pk", flat=True)
    )
    for campaign_id in ids:
        send_campaign.delay(campaign_id)
    return len(ids)
