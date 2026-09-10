"""Celery tasks for email provisioning and maintenance.

All heavy provider calls are handled here rather than in Django views, so HTTP
requests return immediately and retries happen transparently.

Queues:
  email      â€” mailbox/domain/alias provisioning tasks
  outbound   â€” send_email, send_bulk_recipient_email
  campaigns  â€” dispatch_campaign
  webhooks   â€” deliver_webhook
  celery     â€” prune_* maintenance tasks

ProvisioningJob is created by the caller before dispatching the task, then
updated here as the task runs (PENDING â†’ RUNNING â†’ SUCCESS | FAILED | RETRYING).
"""
from __future__ import annotations

import json
import logging

import requests
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from apps.email.exceptions import EmailProviderError
from apps.email.models import (
    BulkEmailCampaign,
    BulkEmailRecipient,
    EmailMessage,
    ProvisioningJob,
    WebhookDelivery,
)
from apps.email.providers import get_mail_provider, get_send_provider
from apps.email.services import render_template, validate_variables
from apps.email.types import OutboundEmail
from apps.email.webhooks import EVENT_HEADER, SIGNATURE_HEADER, build_signature_header

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_DELAY_BASE = 2  # seconds (exponential base)
_RETRY_DELAY_MULTIPLIER = 2  # exponential growth

_WEBHOOK_MAX_RETRIES = 6
_WEBHOOK_RETRY_DELAY_BASE = 10  # seconds (webhook retry base, longer than email)
_WEBHOOK_TIMEOUT_SECONDS = 10

_CAMPAIGN_CHUNK_SIZE = 500

# FAILED-spike alerting — a high share of terminal sends failing over a short
# window points at an infra/provider problem (bad credentials, SES pause, relay
# down), distinct from per-account bounce reputation.
_FAILURE_SPIKE_WINDOW_MINUTES = 60
_FAILURE_SPIKE_MIN_VOLUME = 50
_FAILURE_SPIKE_THRESHOLD = 0.20  # 20% of terminal sends FAILED
_FAILURE_SPIKE_ALERT_COOLDOWN_SECONDS = 3600


def _exponential_backoff_delay(retry_count: int, base: int = _RETRY_DELAY_BASE, multiplier: int = _RETRY_DELAY_MULTIPLIER) -> int:
    """Calculate exponential backoff delay: base * (multiplier ** retry_count).

    Examples:
      retry 0 -> 2 sec
      retry 1 -> 4 sec
      retry 2 -> 8 sec
    """
    return base * (multiplier ** retry_count)


# â”€â”€ Domain provisioning â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@shared_task(
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_RETRY_DELAY_BASE,
    queue="email",
)
def provision_domain_async(
    self, domain_record_id: int, job_id: int | None = None
) -> None:
    """Provision a domain on the mail server (async path for slow operations)."""
    from apps.email.models import EmailDomain
    from apps.email.services import DomainService

    job = _get_job(job_id)
    if job:
        job.celery_task_id = self.request.id or ""
        job.mark_running()

    try:
        domain_record = EmailDomain.objects.select_related("account").get(
            pk=domain_record_id
        )
    except EmailDomain.DoesNotExist:
        if job:
            job.mark_failed("EmailDomain record not found.")
        return

    try:
        DomainService(domain_record.account).provision(domain_record)
        if job:
            job.mark_success()
    except EmailProviderError as exc:
        is_last = self.request.retries >= _MAX_RETRIES
        if job:
            job.mark_failed(str(exc), retrying=not is_last)
        raise self.retry(exc=exc)


# â”€â”€ Email sending â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _send_sandbox_message(msg: EmailMessage, text_body: str, html_body: str) -> None:
    from apps.email.providers.sandbox import SandboxSendProvider, followup_events
    from apps.logs.services import record_message_event

    result = SandboxSendProvider().send(OutboundEmail(
        from_email=msg.from_email,
        to_email=msg.to_email,
        subject=msg.subject,
        text_body=text_body,
        html_body=html_body,
        headers={},
    ))
    if not result.success:
        msg.mark_failed(result.error or "sandbox failure")
        return

    msg.mark_sent(result.provider_message_id)
    for event_type in followup_events(msg.to_email):
        record_message_event(msg, event_type, source="sandbox")


def _send_email_message(
    task, msg: EmailMessage, text_body: str, html_body: str, *, attachments=None
) -> None:
    """Shared send logic for both send_email and send_bulk_recipient_email.

    ``task`` is the bound Celery task instance (for retry/request.retries).
    """
    from apps.email.services.suppression import is_suppressed
    from apps.email.services.reputation import check_can_send, record_send

    att_objs = ()
    if attachments:
        from apps.email.services.attachments import decode_from_task

        att_objs = tuple(decode_from_task(attachments))

    # Test-mode messages route through the sandbox: no suppression / reputation /
    # quota, nothing delivered, deterministic follow-up events.
    if getattr(msg, "key_mode", "live") == "test":
        _send_sandbox_message(msg, text_body, html_body)
        return

    # Final suppression gate before sending (catch-all for race conditions)
    if is_suppressed(msg.account, msg.to_email):
        logger.info("Refusing to send: %s is suppressed", msg.to_email)
        msg.mark_failed("Recipient is suppressed (bounce, complaint, or unsubscribe)")
        return

    # Reputation circuit breaker — a halted account's marketing/transactional
    # mail is blocked (system mail goes via send_system_email, which is exempt).
    # Retrying won't help, so mark failed and return without raising.
    allowed, reason = check_can_send(msg.account)
    if not allowed:
        logger.warning("Reputation halt: dropping send for account=%s (%s)", msg.account_id, reason)
        msg.mark_failed(f"Sender reputation halt: {reason}")
        if msg.campaign_id:
            msg.campaign.increment_counts(failed=1)
            BulkEmailRecipient.objects.filter(message=msg).update(
                status=BulkEmailRecipient.Status.FAILED, error=f"Sender reputation halt: {reason}"[:5000]
            )
            _maybe_complete_campaign(msg.campaign)
        try:
            from apps.billing.limits import LimitChecker

            LimitChecker(msg.account).release_email()
        except Exception:
            logger.exception("reputation halt: quota release failed for %s", msg.pk)
        return

    if html_body:
        try:
            from apps.billing.limits import LimitChecker

            if LimitChecker(msg.account).has_feature("tracking_webhooks"):
                from apps.email.services import apply_tracking

                domain = (
                    msg.domain.domain
                    if msg.domain
                    else msg.from_email.rsplit("@", 1)[-1]
                )
                html_body = apply_tracking(html_body, msg, msg.to_email, domain)
        except Exception as exc:
            logger.debug("_send_email_message: tracking injection skipped: %s", exc)

    headers: dict[str, str] = {}
    if msg.campaign_id:
        # Bulk / marketing mail: attach RFC 8058 one-click unsubscribe headers
        # (Gmail/Yahoo 2024 bulk-sender requirement). Never let this block a send.
        try:
            from apps.email.services.unsubscribe import build_list_unsubscribe_headers

            headers = build_list_unsubscribe_headers(
                msg.account, msg.to_email, campaign_id=msg.campaign_id
            )
        except Exception:
            logger.exception(
                "_send_email_message: List-Unsubscribe header build failed for %s", msg.pk
            )

    try:
        result = get_send_provider().send(OutboundEmail(
            from_email=msg.from_email,
            to_email=msg.to_email,
            subject=msg.subject,
            text_body=text_body,
            html_body=html_body,
            headers=headers,
            attachments=att_objs,
        ))
        msg.mark_sent(result.provider_message_id)
        record_send(msg.account)
        if msg.campaign_id:
            msg.campaign.increment_counts(sent=1)
            BulkEmailRecipient.objects.filter(message=msg).update(
                status=BulkEmailRecipient.Status.SENT
            )
            _maybe_complete_campaign(msg.campaign)
    except Exception as exc:
        msg.mark_failed(str(exc))
        logger.exception("_send_email_message: failed for EmailMessage %s", msg.pk)
        is_last = task.request.retries >= _MAX_RETRIES
        if is_last:
            # Quota was reserved at accept time (LimitChecker.check_email); a
            # message that never gets delivered shouldn't permanently burn it.
            try:
                from apps.billing.limits import LimitChecker

                LimitChecker(msg.account).release_email()
            except Exception:
                logger.exception(
                    "_send_email_message: failed to release quota for EmailMessage %s",
                    msg.pk,
                )
            if msg.campaign_id:
                msg.campaign.increment_counts(failed=1)
                BulkEmailRecipient.objects.filter(message=msg).update(
                    status=BulkEmailRecipient.Status.FAILED, error=str(exc)[:5000]
                )
                _maybe_complete_campaign(msg.campaign)
            # Retries are exhausted and the message is permanently failed —
            # a spike of these is an infra problem, so page the operators.
            try:
                from apps.billing.slack import post_message

                post_message(
                    f":warning: Email send failed after {_MAX_RETRIES} retries — "
                    f"EmailMessage {msg.pk} (account {msg.account_id}, to {msg.to_email}): {exc}"
                )
            except Exception:
                logger.exception("retry-exhaustion Slack alert failed for %s", msg.pk)
        delay = _exponential_backoff_delay(task.request.retries)
        raise task.retry(exc=exc, countdown=delay)


def _maybe_complete_campaign(campaign: BulkEmailCampaign) -> None:
    """Mark a campaign COMPLETED once no recipients are PENDING/QUEUED.

    Called after each recipient's terminal send outcome â€” dispatch_campaign
    only re-enqueues itself while PENDING rows remain, so the final
    QUEUED -> SENT/FAILED transitions (which happen asynchronously, after the
    last chunk was dispatched) are what actually close out the campaign.
    """
    still_open = BulkEmailRecipient.objects.filter(
        campaign=campaign,
        status__in=[BulkEmailRecipient.Status.PENDING, BulkEmailRecipient.Status.QUEUED],
    ).exists()
    if not still_open:
        campaign.mark_completed()


@shared_task(
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_RETRY_DELAY_BASE,
    queue="outbound",
)
def send_email(
    self, email_message_id: int, text_body: str = "", html_body: str = "",
    attachments=None,
) -> None:
    """Send a queued EmailMessage and record the outcome."""
    try:
        msg = EmailMessage.objects.select_related("account", "campaign").get(
            pk=email_message_id
        )
    except EmailMessage.DoesNotExist:
        logger.error("send_email: EmailMessage %s not found", email_message_id)
        return

    if msg.status == EmailMessage.Status.SENT:
        return

    _send_email_message(self, msg, text_body, html_body, attachments=attachments)


@shared_task(
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_RETRY_DELAY_BASE,
    queue="outbound",
)
def send_bulk_recipient_email(self, email_message_id: int) -> None:
    """Render the EmailMessage's template/recipient variables, then send.

    Same retry/tracking/quota-release behavior as send_email â€” the only
    difference is content is resolved from EmailMessage.template +
    BulkEmailRecipient.variables rather than passed in directly.
    """
    try:
        msg = EmailMessage.objects.select_related(
            "account", "campaign", "template"
        ).get(pk=email_message_id)
    except EmailMessage.DoesNotExist:
        logger.error(
            "send_bulk_recipient_email: EmailMessage %s not found", email_message_id
        )
        return

    if msg.status == EmailMessage.Status.SENT:
        return

    recipient = BulkEmailRecipient.objects.filter(message=msg).first()
    variables = recipient.variables if recipient else {}

    campaign = msg.campaign
    if msg.template_id:
        missing = validate_variables(msg.template, variables)
        if missing:
            logger.warning(
                "send_bulk_recipient_email: template %s missing variables %s for recipient %s",
                msg.template_id, missing, msg.to_email,
            )
        subject, text_body, html_body = render_template(msg.template, variables)
    else:
        from apps.email.services import render_string

        campaign_html = campaign.html_override if campaign else ""
        campaign_text = campaign.text_override if campaign else ""
        campaign_subject = campaign.subject_override if campaign else ""

        subject = render_string(campaign_subject, variables)
        text_body = render_string(campaign_text, variables)
        html_body = render_string(campaign_html, variables)

    msg.subject = subject
    msg.rendered_subject = subject
    msg.rendered_text = text_body
    msg.rendered_html = html_body
    msg.save(update_fields=["subject", "rendered_subject", "rendered_text", "rendered_html"])

    _send_email_message(self, msg, text_body, html_body)


# â”€â”€ Bulk campaign fan-out â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@shared_task(
    bind=True,
    max_retries=_MAX_RETRIES,
    default_retry_delay=_RETRY_DELAY_BASE,
    queue="campaigns",
)
def dispatch_campaign(self, campaign_id: int) -> None:
    """Fan a BulkEmailCampaign out to EmailMessage rows, one bounded chunk at a time.

    Self-chaining: processes up to _CAMPAIGN_CHUNK_SIZE PENDING recipients,
    then re-enqueues itself if more remain. Keeps each task run bounded and
    restart-safe if a worker dies mid-campaign.

    Quota is reserved per chunk (LimitChecker.reserve_bulk) with partial-send
    semantics: recipients beyond the account's remaining monthly quota are
    marked FAILED with a quota-exceeded error, but the rest of the chunk (and
    campaign) still proceeds.
    """
    from apps.billing.limits import LimitChecker

    try:
        campaign = BulkEmailCampaign.objects.select_related("account", "domain", "template").get(
            pk=campaign_id
        )
    except BulkEmailCampaign.DoesNotExist:
        logger.error("dispatch_campaign: BulkEmailCampaign %s not found", campaign_id)
        return

    if campaign.status in (
        BulkEmailCampaign.Status.CANCELLED,
        BulkEmailCampaign.Status.COMPLETED,
        BulkEmailCampaign.Status.PAUSED,
    ):
        return

    # Reputation circuit breaker — stop fanning out a campaign for a halted
    # account. It stays PAUSED (with PENDING recipients intact) until an operator
    # resets reputation and re-dispatches.
    from apps.email.services.reputation import check_can_send

    allowed, reason = check_can_send(campaign.account)
    if not allowed:
        logger.warning("Reputation halt: pausing campaign %s (%s)", campaign.pk, reason)
        campaign.mark_paused(f"Paused: sender reputation halt — {reason}")
        return

    campaign.mark_sending()

    chunk = list(
        BulkEmailRecipient.objects.filter(
            campaign=campaign, status=BulkEmailRecipient.Status.PENDING
        ).order_by("pk")[:_CAMPAIGN_CHUNK_SIZE]
    )

    if not chunk:
        remaining = BulkEmailRecipient.objects.filter(
            campaign=campaign,
            status__in=[BulkEmailRecipient.Status.PENDING, BulkEmailRecipient.Status.QUEUED],
        ).exists()
        if not remaining:
            campaign.mark_completed()
        return

    from apps.email.services.suppression import get_suppressed_emails
    from apps.email.services.validation import validate_recipient

    # Check for suppressed recipients (bounces, complaints, unsubscribes)
    suppressed_emails = get_suppressed_emails(campaign.account, [r.to_email for r in chunk])

    to_process, failed_recipients = [], []
    for r in chunk:
        if r.to_email in suppressed_emails:
            failed_recipients.append((r, "Recipient is suppressed (bounce, complaint, or unsubscribe)."))
        elif not validate_recipient(r.to_email):
            failed_recipients.append((r, "Recipient failed validation (invalid syntax or no MX record)."))
        else:
            to_process.append(r)

    if failed_recipients:
        BulkEmailRecipient.objects.bulk_update(
            [
                BulkEmailRecipient(
                    pk=r.pk,
                    status=BulkEmailRecipient.Status.FAILED,
                    error=error,
                )
                for r, error in failed_recipients
            ],
            ["status", "error"],
        )
        campaign.increment_counts(failed=len(failed_recipients))

    granted = LimitChecker(campaign.account).reserve_bulk(len(to_process))
    to_send, to_fail = to_process[:granted], to_process[granted:]

    if to_fail:
        BulkEmailRecipient.objects.filter(
            pk__in=[r.pk for r in to_fail]
        ).update(
            status=BulkEmailRecipient.Status.FAILED,
            error="Monthly email limit reached.",
        )
        campaign.increment_counts(failed=len(to_fail))

    if to_send:
        messages = [
            EmailMessage(
                account=campaign.account,
                domain=campaign.domain,
                template=campaign.template,
                campaign=campaign,
                from_email=campaign.from_email,
                to_email=r.to_email,
                subject=campaign.template.subject if campaign.template else campaign.subject_override,
            )
            for r in to_send
        ]
        created = EmailMessage.objects.bulk_create(messages)

        for recipient, msg in zip(to_send, created):
            recipient.message = msg
            recipient.status = BulkEmailRecipient.Status.QUEUED
        BulkEmailRecipient.objects.bulk_update(to_send, ["message", "status"])
        campaign.increment_counts(queued=len(to_send))

        message_ids = [m.pk for m in created]
        transaction.on_commit(
            lambda ids=message_ids: [send_bulk_recipient_email.delay(mid) for mid in ids]
        )

    still_pending = BulkEmailRecipient.objects.filter(
        campaign=campaign, status=BulkEmailRecipient.Status.PENDING
    ).exists()
    if still_pending:
        dispatch_campaign.delay(campaign_id)
    else:
        # Remaining QUEUED rows complete asynchronously via
        # _maybe_complete_campaign, called from each recipient's send task.
        _maybe_complete_campaign(campaign)


# â”€â”€ Webhook delivery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@shared_task(
    bind=True,
    max_retries=_WEBHOOK_MAX_RETRIES,
    queue="webhooks",
)
def deliver_webhook(self, delivery_id: int) -> None:
    """POST a signed event payload to a WebhookEndpoint, with exponential backoff."""
    try:
        delivery = WebhookDelivery.objects.select_related("endpoint").get(pk=delivery_id)
    except WebhookDelivery.DoesNotExist:
        logger.error("deliver_webhook: WebhookDelivery %s not found", delivery_id)
        return

    if delivery.status == WebhookDelivery.Status.SUCCEEDED:
        return

    endpoint = delivery.endpoint
    body = json.dumps(delivery.payload).encode()
    signature = build_signature_header(endpoint.signing_secret, body)

    try:
        response = requests.post(
            endpoint.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                SIGNATURE_HEADER: signature,
                EVENT_HEADER: delivery.event_type,
            },
            timeout=_WEBHOOK_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        delivery.mark_succeeded(response.status_code)
        endpoint.record_success()
    except Exception as exc:
        # requests.HTTPError carries a .response with a status code; a
        # connection/timeout failure has none.
        response_obj = getattr(exc, "response", None)
        response_code = response_obj.status_code if response_obj is not None else None

        is_last = self.request.retries >= _WEBHOOK_MAX_RETRIES
        delivery.mark_failed(response_code, exhausted=is_last)
        if is_last:
            disabled = endpoint.record_failure(str(exc))
            logger.error(
                "deliver_webhook: exhausted retries for delivery %s (%s): %s",
                delivery_id, delivery.event_type, exc,
            )
            if disabled:
                try:
                    from apps.billing.slack import post_message

                    post_message(
                        f":electric_plug: Webhook endpoint auto-disabled — "
                        f"{endpoint.url} (account {endpoint.account_id}) after "
                        f"{endpoint.consecutive_failures} consecutive failures: {exc}"
                    )
                except Exception:
                    logger.exception("webhook auto-disable Slack alert failed for %s", endpoint.pk)
            return
        countdown = _exponential_backoff_delay(self.request.retries, base=_WEBHOOK_RETRY_DELAY_BASE, multiplier=_RETRY_DELAY_MULTIPLIER)
        raise self.retry(exc=exc, countdown=countdown)


# â”€â”€ Maintenance â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


@shared_task(queue="celery")
def prune_email_logs() -> int:
    """Delete EmailMessage rows older than each account's plan retention window."""
    from datetime import timedelta

    from apps.billing.models import Subscription

    total = 0
    subs = Subscription.objects.select_related("plan").filter(
        status__in=[Subscription.ACTIVE, Subscription.TRIALING]
    )
    for sub in subs:
        days = getattr(sub.plan, "log_retention_days", 0) or 0
        if days <= 0:
            continue
        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = EmailMessage.objects.filter(
            account_id=sub.account_id, created_at__lt=cutoff
        ).delete()
        total += deleted
    logger.info("prune_email_logs: deleted %d expired rows", total)
    return total


@shared_task(queue="celery")
def prune_tracking_tokens() -> int:
    """Delete stale EmailTrackingToken rows older than 90 days."""
    from datetime import timedelta

    from apps.email.models import EmailTrackingToken

    cutoff = timezone.now() - timedelta(days=90)
    deleted, _ = EmailTrackingToken.objects.filter(created_at__lt=cutoff).delete()
    logger.info("prune_tracking_tokens: deleted %d stale tokens", deleted)
    return deleted


@shared_task(queue="celery")
def alert_on_failure_spike() -> dict:
    """Page operators when the recent send-failure rate spikes.

    Complements the per-account reputation breaker (which watches bounces):
    this watches *send* failures — provider outages, bad SES credentials, an
    SES account pause — across the whole platform over a short window.
    """
    from datetime import timedelta

    from django.core.cache import cache

    since = timezone.now() - timedelta(minutes=_FAILURE_SPIKE_WINDOW_MINUTES)
    terminal = EmailMessage.objects.filter(
        created_at__gte=since,
        status__in=[
            EmailMessage.Status.SENT,
            EmailMessage.Status.DELIVERED,
            EmailMessage.Status.FAILED,
        ],
    )
    total = terminal.count()
    failed = terminal.filter(status=EmailMessage.Status.FAILED).count()
    rate = (failed / total) if total else 0.0
    result = {"total": total, "failed": failed, "rate": round(rate, 4), "alerted": False}

    if total < _FAILURE_SPIKE_MIN_VOLUME or rate < _FAILURE_SPIKE_THRESHOLD:
        return result

    if cache.get("email_failure_spike_alerted"):
        return result  # within cooldown — don't re-page
    cache.set("email_failure_spike_alerted", "1", _FAILURE_SPIKE_ALERT_COOLDOWN_SECONDS)
    result["alerted"] = True

    logger.error(
        "EMAIL FAILURE SPIKE: %d/%d terminal sends FAILED (%.1f%%) in the last %d min",
        failed, total, rate * 100, _FAILURE_SPIKE_WINDOW_MINUTES,
    )
    try:
        from apps.billing.slack import post_message

        post_message(
            f":rotating_light: Email failure spike — {failed}/{total} sends FAILED "
            f"({rate:.0%}) in the last {_FAILURE_SPIKE_WINDOW_MINUTES} min. Check the "
            f"send provider / SES account status."
        )
    except Exception:
        logger.exception("failure-spike Slack alert failed")
    return result


@shared_task(queue="celery")
def reverify_pending_domains() -> int:
    """Re-run the live DNS check for every domain still awaiting verification.

    The customer publishes DNS on their own schedule, so we keep checking until
    ownership (and, for SES, the provider's own status) is satisfied — at which
    point ``refresh_domain`` transitions the domain to VERIFIED. Idempotent:
    re-checking an already-live record is a no-op.
    """
    from apps.email.models import EmailDomain
    from apps.email.verification import refresh_domain

    pending = EmailDomain.objects.filter(status=EmailDomain.Status.PENDING)
    verified = 0
    for domain in pending:
        try:
            refresh_domain(domain)
            if domain.is_verified:
                verified += 1
        except Exception:
            logger.exception("reverify_pending_domains: failed for %s", domain.domain)
    logger.info(
        "reverify_pending_domains: checked %d pending domain(s), %d newly verified",
        pending.count(),
        verified,
    )
    return verified


@shared_task(queue="celery")
def prune_provisioning_jobs() -> int:
    """Delete completed ProvisioningJob rows older than 30 days."""
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(days=30)
    deleted, _ = ProvisioningJob.objects.filter(
        status__in=[ProvisioningJob.Status.SUCCESS, ProvisioningJob.Status.FAILED],
        completed_at__lt=cutoff,
    ).delete()
    logger.info("prune_provisioning_jobs: deleted %d old jobs", deleted)
    return deleted


@shared_task(queue="celery")
def snapshot_deliverability() -> int:
    """Write a daily DeliverabilitySnapshot per account (+ per verified domain)."""
    from apps.accounts.models import Account
    from apps.email.models import EmailDomain
    from apps.email.services.deliverability import snapshot

    written = 0
    account_ids = (
        EmailDomain.objects.filter(status=EmailDomain.Status.VERIFIED)
        .values_list("account_id", flat=True)
        .distinct()
    )
    for account in Account.objects.filter(id__in=list(account_ids)):
        try:
            snapshot(account, None)
            written += 1
            for domain in EmailDomain.objects.filter(
                account=account, status=EmailDomain.Status.VERIFIED
            ):
                snapshot(account, domain)
                written += 1
        except Exception:
            logger.exception("snapshot_deliverability failed for account %s", account.id)
    logger.info("snapshot_deliverability: wrote %d snapshots", written)
    return written





# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _get_job(job_id: int | None) -> ProvisioningJob | None:
    if not job_id:
        return None
    try:
        return ProvisioningJob.objects.get(pk=job_id)
    except ProvisioningJob.DoesNotExist:
        logger.warning("_get_job: ProvisioningJob %s not found", job_id)
        return None


