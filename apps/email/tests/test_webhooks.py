import time
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import (
    EmailDomain,
    EmailMessage,
    WebhookDelivery,
    WebhookEndpoint,
)
from apps.email.tasks import deliver_webhook
from apps.email.webhooks import build_signature_header, enqueue_event, verify_signature


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(
        slug="p", name="P", price_monthly=Decimal("10"),
        email_apis=True, outbound_webhooks=True, max_emails_per_month=-1,
    )
    Subscription.objects.create(
        account=acc, plan=plan, status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )
    return acc


@pytest.fixture
def domain(account):
    return EmailDomain.objects.create(
        account=account, domain="mail.acme.com", status=EmailDomain.Status.VERIFIED,
    )


@pytest.fixture
def message(account, domain):
    return EmailMessage.objects.create(
        account=account, domain=domain,
        from_email="hello@mail.acme.com", to_email="recipient@example.com", subject="Hi",
    )


# --- Signing -------------------------------------------------------------------

def test_signature_round_trips():
    secret = "whsec_test"
    body = b'{"event":"message.sent"}'
    header = build_signature_header(secret, body)
    assert verify_signature(secret, body, header) is True


def test_signature_rejects_tampered_body():
    secret = "whsec_test"
    header = build_signature_header(secret, b'{"event":"message.sent"}')
    assert verify_signature(secret, b'{"event":"tampered"}', header) is False


def test_signature_rejects_wrong_secret():
    header = build_signature_header("whsec_a", b"body")
    assert verify_signature("whsec_b", b"body", header) is False


def test_signature_rejects_expired_timestamp():
    secret = "whsec_test"
    body = b"body"
    old_timestamp = int(time.time()) - 3600
    header = build_signature_header(secret, body, timestamp=old_timestamp)
    assert verify_signature(secret, body, header, tolerance=300) is False


def test_signature_rejects_malformed_header():
    assert verify_signature("whsec_test", b"body", "not-a-valid-header") is False


# --- Fan-out ---------------------------------------------------------------------

@pytest.mark.django_db
def test_enqueue_event_fans_out_only_to_subscribed_active_endpoints(
    account, message, django_capture_on_commit_callbacks
):
    subscribed = WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
    )
    WebhookEndpoint.objects.create(
        account=account, url="https://b.example.com/hook", event_types=["message.failed"],
    )
    WebhookEndpoint.objects.create(
        account=account, url="https://c.example.com/hook",
        event_types=["message.sent"], is_active=False,
    )

    with patch("apps.email.tasks.deliver_webhook.delay") as mock_delay:
        with django_capture_on_commit_callbacks(execute=True):
            ids = enqueue_event(
                "message.sent", account=account, message=message, data={"id": message.pk}
            )

    assert len(ids) == 1
    delivery = WebhookDelivery.objects.get(pk=ids[0])
    assert delivery.endpoint_id == subscribed.pk
    assert delivery.event_type == "message.sent"
    mock_delay.assert_called_once_with(delivery.pk)


@pytest.mark.django_db
def test_enqueue_event_swallows_errors(account, message, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr(
        "apps.email.webhooks.WebhookEndpoint.objects.filter",
        _boom,
    )
    # Must not raise — webhook subsystem failures can't break the send path.
    assert enqueue_event("message.sent", account=account, message=message) == []


# --- Triggers --------------------------------------------------------------------

@pytest.mark.django_db
def test_mark_sent_triggers_webhook_event(account, message):
    WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
    )
    with patch("apps.email.tasks.deliver_webhook.delay"):
        message.mark_sent("provider-msg-id")

    assert WebhookDelivery.objects.filter(event_type="message.sent", message=message).exists()


@pytest.mark.django_db
def test_mark_failed_triggers_webhook_event(account, message):
    WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.failed"],
    )
    with patch("apps.email.tasks.deliver_webhook.delay"):
        message.mark_failed("smtp connection refused")

    assert WebhookDelivery.objects.filter(event_type="message.failed", message=message).exists()


# --- Delivery task -----------------------------------------------------------------

@pytest.mark.django_db
def test_deliver_webhook_marks_succeeded_on_2xx(account, message):
    endpoint = WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
    )
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint, event_type="message.sent", message=message,
        payload={"event": "message.sent", "data": {}},
    )

    class _FakeResponse:
        status_code = 200
        def raise_for_status(self):
            pass

    with patch("apps.email.tasks.requests.post", return_value=_FakeResponse()) as mock_post:
        deliver_webhook(delivery.pk)

    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.Status.SUCCEEDED
    assert delivery.response_code == 200
    headers = mock_post.call_args.kwargs["headers"]
    assert "X-Akilent-Signature" in headers
    assert headers["X-Akilent-Event"] == "message.sent"


@pytest.mark.django_db
def test_deliver_webhook_retries_on_failure(account, message):
    endpoint = WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
    )
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint, event_type="message.sent", message=message,
        payload={"event": "message.sent", "data": {}},
    )

    with patch("apps.email.tasks.requests.post", side_effect=ConnectionError("refused")):
        with pytest.raises(Exception):
            deliver_webhook(delivery.pk)

    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.Status.FAILED
    assert delivery.attempt_count == 1


@pytest.mark.django_db
def test_deliver_webhook_exhausts_after_max_retries(account, message):
    endpoint = WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
    )
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint, event_type="message.sent", message=message,
        payload={"event": "message.sent", "data": {}},
    )

    deliver_webhook.push_request(retries=6)  # == _WEBHOOK_MAX_RETRIES
    try:
        with patch("apps.email.tasks.requests.post", side_effect=ConnectionError("refused")):
            deliver_webhook(delivery.pk)
    finally:
        deliver_webhook.pop_request()

    delivery.refresh_from_db()
    endpoint.refresh_from_db()
    assert delivery.status == WebhookDelivery.Status.EXHAUSTED
    assert endpoint.last_error != ""


@pytest.mark.django_db
def test_exhausted_delivery_increments_consecutive_failures(account, message):
    endpoint = WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
    )
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint, event_type="message.sent", message=message,
        payload={"event": "message.sent", "data": {}},
    )
    deliver_webhook.push_request(retries=6)
    try:
        with patch("apps.email.tasks.requests.post", side_effect=ConnectionError("refused")):
            deliver_webhook(delivery.pk)
    finally:
        deliver_webhook.pop_request()

    endpoint.refresh_from_db()
    assert endpoint.consecutive_failures == 1
    assert endpoint.is_active is True
    assert endpoint.health == "degraded"


@pytest.mark.django_db
def test_endpoint_auto_disables_after_threshold(account):
    endpoint = WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
        consecutive_failures=WebhookEndpoint.AUTO_DISABLE_THRESHOLD - 1,
    )
    disabled = endpoint.record_failure("still down")
    assert disabled is True
    assert endpoint.is_active is False
    assert endpoint.disabled_at is not None
    assert endpoint.health == "disabled"

    # a subsequent success via reactivate clears the counters
    endpoint.reactivate()
    assert endpoint.is_active is True
    assert endpoint.consecutive_failures == 0
    assert endpoint.health == "healthy"


@pytest.mark.django_db
def test_successful_delivery_resets_failure_counter(account, message):
    endpoint = WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
        consecutive_failures=4, last_error="was failing",
    )
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint, event_type="message.sent", message=message,
        payload={"event": "message.sent", "data": {}},
    )

    class _Ok:
        status_code = 200
        def raise_for_status(self):
            pass

    with patch("apps.email.tasks.requests.post", return_value=_Ok()):
        deliver_webhook(delivery.pk)

    endpoint.refresh_from_db()
    assert endpoint.consecutive_failures == 0
    assert endpoint.last_error == ""
    assert endpoint.last_success_at is not None


@pytest.mark.django_db
def test_deliver_webhook_skips_already_succeeded(account, message):
    endpoint = WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
    )
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint, event_type="message.sent", message=message,
        payload={}, status=WebhookDelivery.Status.SUCCEEDED,
    )
    with patch("apps.email.tasks.requests.post") as mock_post:
        deliver_webhook(delivery.pk)
    mock_post.assert_not_called()


# --- Dashboard views ---------------------------------------------------------------

@pytest.mark.django_db
def test_webhook_create_requires_feature(client, account):
    account.subscription.plan.outbound_webhooks = False
    account.subscription.plan.save(update_fields=["outbound_webhooks"])
    client.force_login(account.owner)
    resp = client.post(
        "/email/webhooks/create/",
        {"url": "https://a.example.com/hook", "event_types": ["message.sent"]},
    )
    assert resp.status_code == 302
    assert not WebhookEndpoint.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_webhook_create_and_delete(client, account):
    client.force_login(account.owner)
    resp = client.post(
        "/email/webhooks/create/",
        {"url": "https://a.example.com/hook", "event_types": ["message.sent"]},
        follow=True,
    )
    assert resp.status_code == 200
    endpoint = WebhookEndpoint.objects.get(account=account)
    assert endpoint.event_types == ["message.sent"]

    del_resp = client.post(f"/email/webhooks/{endpoint.pk}/delete/", follow=True)
    assert del_resp.status_code == 200
    assert not WebhookEndpoint.objects.filter(pk=endpoint.pk).exists()


@pytest.mark.django_db
def test_webhook_redeliver_requeues_delivery(
    client, account, message, django_capture_on_commit_callbacks
):
    client.force_login(account.owner)
    endpoint = WebhookEndpoint.objects.create(
        account=account, url="https://a.example.com/hook", event_types=["message.sent"],
    )
    delivery = WebhookDelivery.objects.create(
        endpoint=endpoint, event_type="message.sent", message=message,
        payload={}, status=WebhookDelivery.Status.EXHAUSTED,
    )
    with patch("apps.email.tasks.deliver_webhook.delay") as mock_delay:
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(f"/email/webhooks/deliveries/{delivery.pk}/resend/", follow=True)
    assert resp.status_code == 200
    delivery.refresh_from_db()
    assert delivery.status == WebhookDelivery.Status.PENDING
    mock_delay.assert_called_once_with(delivery.pk)
