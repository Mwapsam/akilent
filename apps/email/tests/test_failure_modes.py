"""Cross-cutting failure-mode coverage for the SES send/ingest paths.

Reputation is the product's foundation, so the unhappy paths — throttling,
provider outages, replayed / out-of-order SNS events, and blanket suppression
enforcement — are exercised here explicitly.
"""

import json
from unittest.mock import patch

import pytest
from django.test import RequestFactory

from apps.accounts.models import Account
from apps.email.exceptions import EmailProviderError
from apps.email.models import (
    EmailMessage,
    ProcessedSnsMessage,
    SuppressionListEntry,
)
from apps.email.ses_webhooks import ses_sns_webhook

TOPIC = "arn:aws:sns:us-east-1:123456789:test-topic"


def _notification(message: dict, sns_message_id: str = "sns-1") -> dict:
    return {
        "Type": "Notification",
        "MessageId": sns_message_id,
        "TopicArn": TOPIC,
        "Message": json.dumps(message),
        "Signature": "sig",
        "SigningCertUrl": "http://example.com/cert",
    }


def _post(payload: dict):
    rf = RequestFactory()
    req = rf.post(
        "/webhooks/ses/", data=json.dumps(payload), content_type="application/json"
    )
    with (
        patch("apps.email.ses_webhooks._verify_sns_signature", return_value=True),
        patch(
            "apps.email.ses_webhooks._get_sns_topic_arn_if_allowed", return_value=TOPIC
        ),
    ):
        return ses_sns_webhook(req)


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.fixture
def message(account):
    return EmailMessage.objects.create(
        account=account,
        provider_message_id="pmid-1",
        from_email="s@acme.com",
        to_email="r@x.com",
        subject="Hi",
        status=EmailMessage.Status.SENT,
    )


# --- SNS replay / out-of-order --------------------------------------------------


@pytest.mark.django_db
def test_sns_replay_is_idempotent(account, message):
    payload = _notification(
        {
            "eventType": "Bounce",
            "bounce": {
                "bounceType": "Permanent",
                "bouncedRecipients": [{"emailAddress": "r@x.com"}],
            },
            "mail": {"messageId": "pmid-1"},
        },
        sns_message_id="dup-1",
    )
    assert _post(payload).status_code == 200
    assert _post(payload).status_code == 200  # replay

    assert SuppressionListEntry.objects.filter(email="r@x.com").count() == 1
    assert SuppressionListEntry.objects.get(email="r@x.com").bounce_count == 1
    assert ProcessedSnsMessage.objects.filter(pk="dup-1").count() == 1


@pytest.mark.django_db
def test_replay_survives_cache_flush(account, message):
    from django.core.cache import cache

    payload = _notification(
        {
            "eventType": "Complaint",
            "complaint": {"complainedRecipients": [{"emailAddress": "r@x.com"}]},
            "mail": {"messageId": "pmid-1"},
        },
        sns_message_id="dup-2",
    )
    assert _post(payload).status_code == 200
    cache.clear()  # cache fast-path gone; DB ledger must still stop the replay
    assert _post(payload).status_code == 200
    assert SuppressionListEntry.objects.filter(email="r@x.com").count() == 1


@pytest.mark.django_db
def test_delivery_after_bounce_does_not_resurrect(account, message):
    message.status = EmailMessage.Status.FAILED
    message.save(update_fields=["status"])

    _post(
        _notification(
            {
                "eventType": "Delivery",
                "mail": {"messageId": "pmid-1", "destination": ["r@x.com"]},
            },
            sns_message_id="del-1",
        )
    )
    message.refresh_from_db()
    assert message.status == EmailMessage.Status.FAILED


@pytest.mark.django_db
def test_reject_marks_message_failed(account, message):
    _post(
        _notification(
            {
                "eventType": "Reject",
                "reject": {"reason": "Bad content"},
                "mail": {"messageId": "pmid-1"},
            },
            sns_message_id="rej-1",
        )
    )
    message.refresh_from_db()
    assert message.status == EmailMessage.Status.FAILED
    assert "rejected" in message.error.lower()


@pytest.mark.django_db
def test_delivery_delay_is_acknowledged_without_state_change(account, message):
    resp = _post(
        _notification(
            {"eventType": "DeliveryDelay", "mail": {"messageId": "pmid-1"}},
            sns_message_id="dly-1",
        )
    )
    assert resp.status_code == 200
    message.refresh_from_db()
    assert message.status == EmailMessage.Status.SENT


# --- Provider outage / throttling --------------------------------------------


@pytest.mark.django_db
def test_provider_outage_retries_and_keeps_message(account, monkeypatch):
    msg = EmailMessage.objects.create(
        account=account,
        from_email="s@acme.com",
        to_email="r@x.com",
        subject="Hi",
    )
    monkeypatch.setattr(
        "apps.email.services.suppression.is_suppressed", lambda a, e: False
    )

    class _Down:
        def send(self, outbound):
            raise EmailProviderError("EndpointConnectionError: could not connect")

    monkeypatch.setattr("apps.email.tasks.get_send_provider", lambda: _Down())

    from apps.email.tasks import _send_email_message

    retried = {}

    class _Task:
        class request:
            retries = 0

        def retry(self, exc=None, countdown=None):
            retried["hit"] = True
            raise RuntimeError("retry")

    with pytest.raises(RuntimeError):
        _send_email_message(_Task(), msg, "t", "")

    assert retried.get("hit") is True
    msg.refresh_from_db()
    assert msg.status == EmailMessage.Status.FAILED  # marked, not deleted
    assert EmailMessage.objects.filter(pk=msg.pk).exists()


@pytest.mark.django_db
def test_retry_exhaustion_pages_operators(account, monkeypatch):
    msg = EmailMessage.objects.create(
        account=account,
        from_email="s@acme.com",
        to_email="r@x.com",
        subject="Hi",
    )
    monkeypatch.setattr(
        "apps.email.services.suppression.is_suppressed", lambda a, e: False
    )
    monkeypatch.setattr(
        "apps.email.tasks.get_send_provider",
        lambda: type(
            "P",
            (),
            {"send": lambda s, o: (_ for _ in ()).throw(EmailProviderError("boom"))},
        )(),
    )
    alerts = []
    monkeypatch.setattr("apps.billing.slack.post_message", lambda t: alerts.append(t))

    from apps.email.tasks import _MAX_RETRIES, _send_email_message

    class _Task:
        class request:
            retries = _MAX_RETRIES  # last attempt

        def retry(self, exc=None, countdown=None):
            raise RuntimeError("retry")

    with pytest.raises(RuntimeError):
        _send_email_message(_Task(), msg, "t", "")

    assert len(alerts) == 1
    assert "after" in alerts[0].lower()


# --- Suppression enforcement is total ---------------------------------------


@pytest.mark.django_db
def test_every_send_path_refuses_a_suppressed_address(account, monkeypatch):
    SuppressionListEntry.objects.create(
        account=account,
        email="blocked@x.com",
        reason=SuppressionListEntry.Reason.BOUNCE,
    )

    # 1) task-level shared send body
    from apps.email.tasks import _send_email_message

    msg = EmailMessage.objects.create(
        account=account,
        from_email="s@acme.com",
        to_email="blocked@x.com",
        subject="Hi",
    )
    sent = []
    monkeypatch.setattr(
        "apps.email.tasks.get_send_provider",
        lambda: type("P", (), {"send": lambda s, o: sent.append(o)})(),
    )

    class _Task:
        class request:
            retries = 0

    _send_email_message(_Task(), msg, "t", "")
    msg.refresh_from_db()
    assert msg.status == EmailMessage.Status.FAILED
    assert sent == []

    # 2) system email path (global suppression)
    from apps.email.services.suppression import is_suppressed_globally

    assert is_suppressed_globally("blocked@x.com") is True

    from apps.email.services import send as send_mod

    provider_calls = []
    monkeypatch.setattr(
        send_mod,
        "get_send_provider",
        lambda: type("P", (), {"send": lambda s, o: provider_calls.append(o)})(),
        raising=False,
    )
    send_mod.send_system_email("blocked@x.com", "S", "body")
    assert provider_calls == []


# --- Provider acceptance is the irreversible boundary (B2) -------------------
#
# Once the provider has accepted a message, nothing afterwards may send it
# again: not a bookkeeping error, and not a redelivered task arriving after
# SNS has already moved the message on to DELIVERED/OPENED/BOUNCED.


class _Accepting:
    """A provider that accepts every message and counts the calls."""

    def __init__(self):
        self.calls = 0

    def send(self, outbound):
        from apps.email.types import SendResult

        self.calls += 1
        return SendResult(success=True, provider_message_id=f"ses-{self.calls}")


class _NoRetryTask:
    class request:
        retries = 0

    def retry(self, exc=None, countdown=None):  # pragma: no cover - must not happen
        raise AssertionError(f"retry after the provider accepted the message: {exc!r}")


@pytest.fixture
def campaign_message(account):
    from apps.email.models import BulkEmailCampaign, BulkEmailRecipient, EmailDomain

    domain = EmailDomain.objects.create(
        account=account, domain="acme.com", status=EmailDomain.Status.VERIFIED
    )
    campaign = BulkEmailCampaign.objects.create(
        account=account,
        domain=domain,
        from_email="news@acme.com",
        subject_override="Hi",
        recipient_count=1,
        status=BulkEmailCampaign.Status.SENDING,
    )
    msg = EmailMessage.objects.create(
        account=account,
        domain=domain,
        campaign=campaign,
        from_email="news@acme.com",
        to_email="r@x.com",
        subject="Hi",
    )
    BulkEmailRecipient.objects.create(
        campaign=campaign,
        to_email="r@x.com",
        message=msg,
        status=BulkEmailRecipient.Status.QUEUED,
    )
    return msg


@pytest.fixture
def accepting(monkeypatch):
    provider = _Accepting()
    monkeypatch.setattr("apps.email.tasks.get_send_provider", lambda: provider)
    monkeypatch.setattr(
        "apps.email.services.suppression.is_suppressed", lambda a, e: False
    )
    return provider


@pytest.mark.django_db
def test_bookkeeping_failure_after_acceptance_never_retries(
    campaign_message, accepting, monkeypatch
):
    """record_send blowing up must not re-send, and must not skip later steps."""
    from apps.email.tasks import _send_email_message

    def _boom(account):
        raise RuntimeError("reputation store down")

    monkeypatch.setattr("apps.email.services.reputation.record_send", _boom)

    _send_email_message(_NoRetryTask(), campaign_message, "t", "")

    assert accepting.calls == 1
    campaign_message.refresh_from_db()
    assert campaign_message.status == EmailMessage.Status.SENT
    assert campaign_message.provider_message_id == "ses-1"
    # The campaign step ran even though record_send (the step before) failed.
    campaign_message.campaign.refresh_from_db()
    assert campaign_message.campaign.sent_count == 1


@pytest.mark.django_db
def test_mark_sent_failing_after_its_save_still_records_the_provider_id(
    campaign_message, accepting, monkeypatch
):
    """mark_sent saves, then fans out the event; the fan-out raising must not retry."""
    from apps.email.tasks import _send_email_message

    def _explode(self, *a, **k):
        raise RuntimeError("webhook fan-out failed")

    monkeypatch.setattr(EmailMessage, "_record_event", _explode)

    _send_email_message(_NoRetryTask(), campaign_message, "t", "")

    assert accepting.calls == 1
    row = EmailMessage.objects.get(pk=campaign_message.pk)
    assert row.status == EmailMessage.Status.SENT
    # Stored, so SNS events can be matched and a re-run is refused.
    assert row.provider_message_id == "ses-1"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [
        EmailMessage.Status.SENT,
        EmailMessage.Status.DELIVERED,
        EmailMessage.Status.OPENED,
        EmailMessage.Status.BOUNCED,
    ],
)
def test_redelivered_task_never_calls_the_provider(campaign_message, accepting, status):
    """A stale task arriving after SNS moved the message on must not resend it."""
    from apps.email.tasks import send_bulk_recipient_email, send_email

    EmailMessage.objects.filter(pk=campaign_message.pk).update(
        status=status, provider_message_id="ses-original"
    )
    campaign = campaign_message.campaign
    campaign.refresh_from_db()
    before = campaign.sent_count

    send_email(campaign_message.pk, "t", "")
    send_bulk_recipient_email(campaign_message.pk)

    assert accepting.calls == 0
    campaign.refresh_from_db()
    assert campaign.sent_count == before


@pytest.mark.django_db
def test_a_provider_id_alone_blocks_a_resend(campaign_message, accepting):
    """Even with a status that doesn't say so, a stored provider id means accepted."""
    from apps.email.tasks import send_email

    EmailMessage.objects.filter(pk=campaign_message.pk).update(
        status=EmailMessage.Status.QUEUED, provider_message_id="ses-original"
    )
    send_email(campaign_message.pk, "t", "")
    assert accepting.calls == 0


@pytest.mark.django_db
def test_failed_message_never_accepted_is_still_sent(campaign_message, accepting):
    """FAILED is also the between-retries state; the guard must let it through."""
    from apps.email.tasks import send_email

    EmailMessage.objects.filter(pk=campaign_message.pk).update(
        status=EmailMessage.Status.FAILED, provider_message_id=None
    )
    send_email(campaign_message.pk, "t", "")
    assert accepting.calls == 1


@pytest.mark.django_db
def test_campaign_is_counted_once_when_after_send_runs_twice(campaign_message):
    """Bookkeeping is idempotent even if it is somehow reached a second time."""
    from apps.email.tasks import _after_send
    from apps.email.types import SendResult

    result = SendResult(success=True, provider_message_id="ses-1")
    _after_send(campaign_message, result)
    _after_send(campaign_message, result)

    campaign_message.campaign.refresh_from_db()
    assert campaign_message.campaign.sent_count == 1
