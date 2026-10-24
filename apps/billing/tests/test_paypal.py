"""PayPal provider (Orders v2 checkout, pay-link renewals) and its REST client.

Order and capture bodies come from real sandbox responses recorded in
docs/billing/paypal/fixtures/sandbox/, with the order id and our reference swapped in, so the
code is tested against the shapes PayPal actually returns (e.g. an order COMPLETED while its
capture is PENDING review, and a minimal capture response with no payee)."""

import copy
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings

from apps.accounts.models import Membership
from apps.billing import payments, renewals
from apps.billing.models import BillingPayment, ProcessedWebhookEvent, Subscription
from apps.billing.paypal import PayPalClient, PayPalError, PayPalUncertain
from apps.billing.providers import registry
from apps.core.models import SiteSettings

FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "billing"
    / "paypal"
    / "fixtures"
    / "sandbox"
)
CLIENT = "apps.billing.providers.paypal.get_paypal_client"
MERCHANT = "CXZJKF9LDQBRY"  # the sandbox business account the fixtures were paid to
SANDBOX_ORDER = "9CD35462JE6325649"
SANDBOX_REF = "probe_aa77aa1f512cv"

PAYPAL_SETTINGS = dict(
    PAYPAL_ENV="sandbox",
    PAYPAL_ALLOW_SANDBOX=True,
    PAYPAL_CLIENT_ID="test-client",
    PAYPAL_CLIENT_SECRET="test-secret",
    PAYPAL_WEBHOOK_ID="WH-TEST",
    PAYPAL_MERCHANT_ID=MERCHANT,
    PAYPAL_CURRENCIES=frozenset({"USD"}),
    BILLING_CURRENCY="USD",
)


def _fixture(name, *, order_id=None, reference=None):
    """A recorded sandbox body, re-pointed at our test order and payment reference."""
    text = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
    if order_id:
        text = text.replace(SANDBOX_ORDER, order_id)
    if reference:
        text = text.replace(SANDBOX_REF, reference)
    return json.loads(text)["body"]


def _set_capture(order, **fields):
    order = copy.deepcopy(order)
    order["purchase_units"][0]["payments"]["captures"][0].update(fields)
    return order


@pytest.fixture
def paypal(db, settings):
    for key, value in PAYPAL_SETTINGS.items():
        setattr(settings, key, value)
    registry.reset()
    site = SiteSettings.load()
    site.billing_provider = "paypal"
    site.payments_enabled = True
    site.save()
    yield registry.get("paypal")
    registry.reset()


@pytest.fixture
def owner(account):
    user = User.objects.create_user(username="pp-owner", password="pw12345!")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


@pytest.fixture
def pending(account, plan, subscription, paypal):
    payment = payments.create_payment(
        account, plan, Subscription.MONTHLY, provider="paypal"
    )
    payment.provider_operation_id = "ORDER123"
    payment.provider_request_id = f"order-{payment.reference}"
    payment.save()
    return payment


def _captured(payment):
    """The full order after capture (COMPLETED capture), as GET returns it."""
    order = _fixture(
        "order_get_vault_after_capture",
        order_id=payment.provider_operation_id,
        reference=payment.reference,
    )
    return _set_capture(order, status="COMPLETED", status_details=None)


def _client(**methods):
    client = MagicMock()
    for name, value in methods.items():
        mock = getattr(client, name)
        if (
            isinstance(value, Exception)
            or callable(value)
            and not isinstance(value, dict)
        ):
            mock.side_effect = value
        else:
            mock.return_value = value
    return client


def _return(client_http, payment, token=None):
    token = token or payment.provider_operation_id
    return client_http.get(
        f"/billing/return/paypal/?ref={payment.reference}&token={token}"
    )


# --- configuration -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_not_ready_until_every_key_is_set(paypal, settings):
    assert paypal.is_configured()
    assert paypal.config_status() == ""
    settings.PAYPAL_WEBHOOK_ID = ""
    assert not paypal.is_configured()
    assert "PAYPAL_WEBHOOK_ID" in paypal.config_status()


@pytest.mark.django_db
def test_currency_outside_the_allowlist_is_flagged(paypal, settings):
    settings.BILLING_CURRENCY = "ZMW"
    assert not paypal.supports_currency("ZMW")
    assert "ZMW" in paypal.config_status()


@pytest.mark.django_db
def test_paypal_never_charges_saved_methods(paypal):
    assert paypal.off_session_charge is False
    assert paypal.account_key() == f"sandbox:{MERCHANT}"
    assert paypal.checkout_label == "Pay with PayPal"


# --- checkout ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_checkout_creates_an_order_for_our_reference(
    client, owner, plan, subscription, paypal
):
    created = _fixture("order_create_plain")
    pp = _client(create_order=created)
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        resp = client.get("/billing/checkout/?plan=starter&period=monthly")

    payment = BillingPayment.objects.get(provider="paypal")
    assert resp.status_code == 302
    assert (
        resp["Location"]
        == f"https://www.sandbox.paypal.com/checkoutnow?token={created['id']}"
    )
    body = pp.create_order.call_args.args[0]
    unit = body["purchase_units"][0]
    assert unit["custom_id"] == unit["invoice_id"] == payment.reference
    assert unit["amount"] == {"currency_code": "USD", "value": "19.00"}
    assert (
        pp.create_order.call_args.kwargs["request_id"] == f"order-{payment.reference}"
    )
    assert "vault" not in json.dumps(body)
    assert payment.provider_operation_id == created["id"]


@pytest.mark.django_db
def test_checkout_failure_sends_the_customer_back(
    client, owner, plan, subscription, paypal
):
    client.force_login(owner)
    with patch(CLIENT, return_value=_client(create_order=PayPalError("x", status=422))):
        resp = client.get("/billing/checkout/?plan=starter&period=monthly")
    assert resp["Location"] == "/billing/plans/"
    assert BillingPayment.objects.get(provider="paypal").provider_operation_id == ""


@pytest.mark.django_db
def test_checkout_refuses_an_unsupported_currency(
    client, owner, plan, subscription, paypal, settings
):
    settings.PAYPAL_CURRENCIES = frozenset({"EUR"})
    pp = _client()
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        resp = client.get("/billing/checkout/?plan=starter&period=monthly")
    assert resp["Location"] == "/billing/plans/"
    pp.create_order.assert_not_called()


# --- return ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_return_captures_and_activates(client, owner, pending, subscription):
    order = _captured(pending)
    pp = _client(capture_order=order, get_order=order)
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)

    pending.refresh_from_db()
    subscription.refresh_from_db()
    assert pending.status == BillingPayment.SUCCEEDED
    assert subscription.status == Subscription.ACTIVE
    assert (
        pp.capture_order.call_args.kwargs["request_id"]
        == f"capture-{pending.reference}"
    )


@pytest.mark.django_db
def test_completed_order_with_pending_capture_is_not_paid(
    client, owner, pending, subscription
):
    """Recorded in the sandbox: order COMPLETED, capture PENDING (PENDING_REVIEW)."""
    order = _fixture(
        "order_get_vault_after_capture",
        order_id="ORDER123",
        reference=pending.reference,
    )
    assert order["status"] == "COMPLETED"
    pp = _client(capture_order=order, get_order=order)
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        resp = _return(client, pending)

    pending.refresh_from_db()
    assert pending.status == BillingPayment.PENDING
    assert resp.status_code == 200  # the "confirming your payment" page


@pytest.mark.django_db
def test_minimal_capture_response_is_checked_against_the_full_order(
    client, owner, pending, subscription
):
    """The recorded capture response has no payee; the order is read to check it."""
    minimal = _set_capture(
        _fixture("capture_vault", order_id="ORDER123", reference=pending.reference),
        status="COMPLETED",
    )
    assert "payee" not in minimal["purchase_units"][0]
    pp = _client(capture_order=minimal, get_order=_captured(pending))
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
    pending.refresh_from_db()
    assert pending.status == BillingPayment.SUCCEEDED
    pp.get_order.assert_called_with("ORDER123")


@pytest.mark.django_db
def test_return_for_another_order_is_ignored(client, owner, pending, subscription):
    pp = _client()
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending, token="SOMEONE_ELSES_ORDER")
    pp.capture_order.assert_not_called()
    pending.refresh_from_db()
    assert pending.status == BillingPayment.PENDING


@pytest.mark.django_db
@pytest.mark.parametrize(
    "error",
    [
        PayPalError("dup", status=422, issue="ORDER_ALREADY_CAPTURED"),
        PayPalUncertain("timeout"),
    ],
)
def test_already_captured_or_timed_out_capture_is_read_back(
    client, owner, pending, subscription, error
):
    pp = _client(capture_order=error, get_order=_captured(pending))
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
    pending.refresh_from_db()
    assert pending.status == BillingPayment.SUCCEEDED


@pytest.mark.django_db
def test_duplicate_invoice_is_held_not_declined(client, owner, pending, subscription):
    """Recorded in the sandbox: a second capture for the same invoice id is refused."""
    pp = _client(
        capture_order=PayPalError("dup", status=422, issue="DUPLICATE_INVOICE_ID")
    )
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
    pending.refresh_from_db()
    assert pending.status == BillingPayment.PENDING
    assert pending.resolution == BillingPayment.UNRESOLVED


@pytest.mark.django_db
def test_unapproved_order_stays_pending(client, owner, pending, subscription):
    pp = _client(
        capture_order=PayPalError("no", status=422, issue="ORDER_NOT_APPROVED")
    )
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
    pending.refresh_from_db()
    assert pending.status == BillingPayment.PENDING
    assert pending.resolution == ""


@pytest.mark.django_db
@pytest.mark.parametrize(
    "tamper",
    [
        lambda o: o["purchase_units"][0]["payee"].update(merchant_id="SOMEONEELSE"),
        lambda o: o["purchase_units"][0]["payments"]["captures"][0].update(
            custom_id="pay_x"
        ),
        lambda o: o["purchase_units"][0]["payments"]["captures"][0].update(
            invoice_id="pay_x"
        ),
        lambda o: o.update(id="OTHER_ORDER"),
    ],
    ids=["payee", "custom_id", "invoice_id", "order_id"],
)
def test_money_that_isnt_this_payments_never_activates(
    client, owner, pending, subscription, tamper
):
    order = _captured(pending)
    tamper(order)
    pp = _client(capture_order=order, get_order=order)
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
    pending.refresh_from_db()
    assert pending.status != BillingPayment.SUCCEEDED


@pytest.mark.django_db
def test_short_payment_fails(client, owner, pending, subscription):
    order = _set_capture(
        _captured(pending), amount={"currency_code": "USD", "value": "1.00"}
    )
    pp = _client(capture_order=order, get_order=order)
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
    pending.refresh_from_db()
    assert pending.status == BillingPayment.FAILED


@pytest.mark.django_db
def test_unknown_capture_status_is_never_paid(client, owner, pending, subscription):
    order = _set_capture(_captured(pending), status="SOMETHING_NEW")
    pp = _client(capture_order=order, get_order=order)
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
    pending.refresh_from_db()
    assert pending.status == BillingPayment.PENDING


# --- webhooks ----------------------------------------------------------------------------------

HEADERS = {
    "HTTP_PAYPAL_AUTH_ALGO": "SHA256withRSA",
    "HTTP_PAYPAL_CERT_URL": "https://api.sandbox.paypal.com/v1/notifications/certs/x",
    "HTTP_PAYPAL_TRANSMISSION_ID": "t-1",
    "HTTP_PAYPAL_TRANSMISSION_SIG": "sig",
    "HTTP_PAYPAL_TRANSMISSION_TIME": "2026-10-24T10:00:00Z",
}


def _event(kind, resource, event_id="WH-EVT-1"):
    return {"id": event_id, "event_type": kind, "resource": resource}


def _post(http, event, headers=HEADERS):
    return http.post(
        "/billing/webhooks/paypal/",
        data=json.dumps(event),
        content_type="application/json",
        **headers,
    )


@pytest.mark.django_db
def test_webhook_needs_headers_and_json(client, paypal):
    with patch(CLIENT, return_value=_client(verify_webhook_signature=True)):
        assert _post(client, _event("X", {}), headers={}).status_code == 401
        resp = client.post(
            "/billing/webhooks/paypal/",
            data="nope",
            content_type="application/json",
            **HEADERS,
        )
        assert resp.status_code == 401


@pytest.mark.django_db
def test_webhook_with_a_bad_signature_is_rejected(client, paypal):
    pp = _client(verify_webhook_signature=False)
    with patch(CLIENT, return_value=pp):
        assert _post(client, _event("X", {})).status_code == 401
    sent = pp.verify_webhook_signature.call_args.args[0]
    assert sent["webhook_id"] == "WH-TEST"
    assert sent["transmission_id"] == "t-1"


@pytest.mark.django_db
def test_webhook_without_a_webhook_id_is_rejected(client, paypal, settings):
    settings.PAYPAL_WEBHOOK_ID = ""
    with patch(CLIENT, return_value=_client(verify_webhook_signature=True)):
        assert _post(client, _event("X", {})).status_code == 401


@pytest.mark.django_db
def test_paypal_outage_during_verification_asks_for_a_retry(client, paypal):
    with patch(
        CLIENT, return_value=_client(verify_webhook_signature=PayPalUncertain("5xx"))
    ):
        assert _post(client, _event("X", {})).status_code == 500


@pytest.mark.django_db
def test_order_approved_captures_once(client, pending, subscription):
    order = _captured(pending)
    pp = _client(verify_webhook_signature=True, capture_order=order, get_order=order)
    event = _event("CHECKOUT.ORDER.APPROVED", {"id": "ORDER123"})
    with patch(CLIENT, return_value=pp):
        assert _post(client, event).status_code == 200
        assert _post(client, event).status_code == 200  # PayPal's retry

    pending.refresh_from_db()
    assert pending.status == BillingPayment.SUCCEEDED
    assert pp.capture_order.call_count == 1
    assert ProcessedWebhookEvent.objects.filter(
        event_key="paypal:CHECKOUT.ORDER.APPROVED:WH-EVT-1"
    ).exists()


def _capture_resource(payment, status="COMPLETED"):
    capture = _captured(payment)["purchase_units"][0]["payments"]["captures"][0]
    capture = copy.deepcopy(capture)
    capture["status"] = status
    capture["supplementary_data"] = {
        "related_ids": {"order_id": payment.provider_operation_id}
    }
    return capture


@pytest.mark.django_db
def test_pending_capture_completes_later_by_webhook(client, pending, subscription):
    capture = _capture_resource(pending)
    pp = _client(verify_webhook_signature=True, get_capture=capture)
    with patch(CLIENT, return_value=pp):
        _post(client, _event("PAYMENT.CAPTURE.COMPLETED", {"id": capture["id"]}))
    pending.refresh_from_db()
    subscription.refresh_from_db()
    assert pending.status == BillingPayment.SUCCEEDED
    assert subscription.status == Subscription.ACTIVE
    pp.get_capture.assert_called_with(
        capture["id"]
    )  # re-fetched, the body isn't trusted


@pytest.mark.django_db
def test_late_denied_event_does_not_undo_a_paid_payment(client, pending, subscription):
    pp = _client(verify_webhook_signature=True, get_capture=_capture_resource(pending))
    with patch(CLIENT, return_value=pp):
        _post(client, _event("PAYMENT.CAPTURE.COMPLETED", {"id": "C1"}, "E1"))
    pp.get_capture.return_value = _capture_resource(pending, status="DECLINED")
    with patch(CLIENT, return_value=pp):
        _post(client, _event("PAYMENT.CAPTURE.DENIED", {"id": "C1"}, "E2"))
    pending.refresh_from_db()
    assert pending.status == BillingPayment.SUCCEEDED


@pytest.mark.django_db
def test_capture_event_for_another_order_is_ignored(client, pending, subscription):
    capture = _capture_resource(pending)
    capture["supplementary_data"]["related_ids"]["order_id"] = "OTHER"
    with patch(
        CLIENT, return_value=_client(verify_webhook_signature=True, get_capture=capture)
    ):
        _post(client, _event("PAYMENT.CAPTURE.COMPLETED", {"id": "C1"}))
    pending.refresh_from_db()
    assert pending.status == BillingPayment.PENDING


def _refund(refund_id, value, reference, status="COMPLETED"):
    return {
        "id": refund_id,
        "status": status,
        "amount": {"currency_code": "USD", "value": value},
        "custom_id": reference,
        "invoice_id": reference,
    }


@pytest.mark.django_db
def test_partial_then_full_refund_by_webhook(client, pending, subscription):
    pp = _client(verify_webhook_signature=True, get_capture=_capture_resource(pending))
    with patch(CLIENT, return_value=pp):
        _post(client, _event("PAYMENT.CAPTURE.COMPLETED", {"id": "C1"}, "E1"))
        pp.get_refund.return_value = _refund("R1", "5.00", pending.reference)
        _post(client, _event("PAYMENT.CAPTURE.REFUNDED", {"id": "R1"}, "E2"))
        pending.refresh_from_db()
        assert pending.refund_state == BillingPayment.REFUND_PARTIAL
        pp.get_refund.return_value = _refund("R2", "14.00", pending.reference)
        _post(client, _event("PAYMENT.CAPTURE.REFUNDED", {"id": "R2"}, "E3"))

    pending.refresh_from_db()
    assert pending.status == BillingPayment.SUCCEEDED
    assert pending.refund_state == BillingPayment.REFUND_FULL
    assert pending.refunded_amount == Decimal("19.00")


@pytest.mark.django_db
def test_refund_found_through_its_capture(client, pending, subscription):
    pp = _client(verify_webhook_signature=True, get_capture=_capture_resource(pending))
    refund = _refund("R1", "5.00", "")
    refund.update(
        custom_id=None,
        invoice_id=None,
        links=[
            {
                "rel": "up",
                "href": "https://api-m.sandbox.paypal.com/v2/payments/captures/C1",
            }
        ],
    )
    with patch(CLIENT, return_value=pp):
        _post(client, _event("PAYMENT.CAPTURE.COMPLETED", {"id": "C1"}, "E1"))
        pp.get_refund.return_value = refund
        _post(client, _event("PAYMENT.CAPTURE.REFUNDED", {"id": "R1"}, "E2"))
    pending.refresh_from_db()
    assert pending.refunded_amount == Decimal("5.00")


@pytest.mark.django_db
def test_refund_not_yet_completed_is_not_recorded(client, pending, subscription):
    pp = _client(
        verify_webhook_signature=True,
        get_refund=_refund("R1", "5.00", pending.reference, "PENDING"),
    )
    with patch(CLIENT, return_value=pp):
        _post(client, _event("PAYMENT.CAPTURE.REFUNDED", {"id": "R1"}))
    assert not pending.refunds.exists()


# --- renewals ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_renewal_goes_to_a_pay_link_not_a_charge(account, plan, subscription, paypal):
    from datetime import timedelta

    from django.utils import timezone

    subscription.status = Subscription.ACTIVE
    subscription.provider = "paypal"
    subscription.current_period_end = timezone.now() - timedelta(hours=1)
    subscription.save()
    pp = _client()
    with patch(CLIENT, return_value=pp):
        assert renewals.renew(subscription.pk) == "past_due"
    subscription.refresh_from_db()
    assert subscription.status == Subscription.PAST_DUE
    pp.create_order.assert_not_called()


# --- the REST client ---------------------------------------------------------------------------


def _response(status, body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.content = json.dumps(body or {}).encode()
    r.headers = headers or {}
    return r


@pytest.fixture
def api(settings):
    cache.clear()
    client = PayPalClient("cid", "secret", "sandbox")
    client._session = MagicMock()
    client._session.post.return_value = _response(
        200, {"access_token": "T1", "expires_in": 32400}
    )
    yield client
    cache.clear()


def test_token_is_cached(api):
    api._session.request.return_value = _response(200, {"id": "O1"})
    api.get_order("O1")
    api.get_order("O1")
    assert api._session.post.call_count == 1


def test_expired_token_is_refreshed_once(api):
    api._session.request.side_effect = [_response(401), _response(200, {"id": "O1"})]
    assert api.get_order("O1") == {"id": "O1"}
    assert api._session.post.call_count == 2


def test_post_timeout_is_uncertain_and_not_retried(api):
    api._session.request.side_effect = requests.Timeout()
    with pytest.raises(PayPalUncertain):
        api.capture_order("O1", request_id="capture-x")
    assert api._session.request.call_count == 1


def test_post_server_error_is_uncertain(api):
    api._session.request.return_value = _response(503, {"name": "SERVICE_UNAVAILABLE"})
    with pytest.raises(PayPalUncertain):
        api.create_order({}, request_id="order-x")
    assert api._session.request.call_count == 1


def test_get_is_retried(api, monkeypatch):
    monkeypatch.setattr("apps.billing.paypal.time.sleep", lambda s: None)
    api._session.request.side_effect = [
        requests.ConnectionError(),
        _response(200, {"id": "O1"}),
    ]
    assert api.get_order("O1") == {"id": "O1"}


def test_error_carries_paypals_issue_code(api):
    body = _fixture("capture_link1_duplicate_invoice")
    api._session.request.return_value = _response(422, body)
    with pytest.raises(PayPalError) as exc:
        api.capture_order("O1", request_id="capture-x")
    assert exc.value.issue == "DUPLICATE_INVOICE_ID"
    assert exc.value.debug_id
    assert not isinstance(exc.value, PayPalUncertain)


def test_request_id_header_is_sent(api):
    api._session.request.return_value = _response(201, {"id": "O1"})
    api.create_order({"intent": "CAPTURE"}, request_id="order-abc")
    headers = api._session.request.call_args.kwargs["headers"]
    assert headers["PayPal-Request-Id"] == "order-abc"
    assert headers["Authorization"] == "Bearer T1"


@override_settings(PAYPAL_ENV="live")
def test_live_uses_the_live_host():
    assert PayPalClient("a", "b", "live").base_url == "https://api-m.paypal.com"


def test_unknown_environment_is_refused():
    with pytest.raises(PayPalError):
        PayPalClient("a", "b", "production")


# --- review fixes ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_empty_environment_is_not_configured(paypal, settings):
    settings.PAYPAL_ENV = ""
    assert not paypal.is_configured()
    assert "PAYPAL_ENV" in paypal.config_status()


@pytest.mark.django_db
def test_sandbox_on_a_production_server_is_refused(paypal, settings):
    settings.DEBUG = False
    settings.PAYPAL_ALLOW_SANDBOX = False
    assert not paypal.is_configured()
    assert "sandbox" in paypal.config_status()
    assert registry.active() is None
    settings.PAYPAL_ENV = "live"
    assert paypal.is_configured()


@pytest.mark.django_db
def test_approval_of_an_abandoned_checkout_is_not_captured(
    client, pending, subscription
):
    BillingPayment.objects.filter(pk=pending.pk).update(status=BillingPayment.ABANDONED)
    pp = _client(verify_webhook_signature=True)
    with patch(CLIENT, return_value=pp):
        assert (
            _post(
                client, _event("CHECKOUT.ORDER.APPROVED", {"id": "ORDER123"})
            ).status_code
            == 200
        )
    pp.capture_order.assert_not_called()


@pytest.mark.django_db
def test_return_to_a_failed_checkout_is_not_captured(
    client, owner, pending, subscription
):
    BillingPayment.objects.filter(pk=pending.pk).update(status=BillingPayment.FAILED)
    pp = _client()
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
    pp.capture_order.assert_not_called()


def _stale(payment):
    from datetime import timedelta

    from django.utils import timezone

    BillingPayment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(days=2)
    )


@pytest.mark.django_db
def test_timed_out_capture_is_marked_sent_then_reconciled(
    client, owner, pending, subscription
):
    approved = _fixture("order_get_vault_approved", order_id="ORDER123")
    assert (
        approved["status"] == "APPROVED"
        and "payments" not in approved["purchase_units"][0]
    )
    pp = _client(capture_order=PayPalUncertain("timeout"), get_order=approved)
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
    pending.refresh_from_db()
    assert pending.status == BillingPayment.PENDING
    assert pending.charge_started_at is not None

    # A day later PayPal shows it was captured after all: reconciled, not abandoned.
    _stale(pending)
    pp.get_order.return_value = _captured(pending)
    with patch(CLIENT, return_value=pp):
        payments.abandon_stale()
    pending.refresh_from_db()
    assert pending.status == BillingPayment.SUCCEEDED


@pytest.mark.django_db
def test_reconcile_of_an_order_never_captured_gives_it_up(pending, subscription):
    payments.mark_charge_started(pending)
    _stale(pending)
    approved = _fixture("order_get_vault_approved", order_id="ORDER123")
    with patch(CLIENT, return_value=_client(get_order=approved)):
        payments.abandon_stale()
    pending.refresh_from_db()
    assert pending.status == BillingPayment.FAILED
    assert pending.resolution == ""


@pytest.mark.django_db
def test_reconcile_that_cant_reach_paypal_holds_the_payment(pending, subscription):
    payments.mark_charge_started(pending)
    _stale(pending)
    with patch(CLIENT, return_value=_client(get_order=PayPalError("down", status=503))):
        payments.abandon_stale()
    pending.refresh_from_db()
    assert pending.status == BillingPayment.PENDING
    assert pending.resolution == BillingPayment.UNRESOLVED


@pytest.mark.django_db
def test_duplicate_invoice_survives_a_new_checkout(
    client, owner, pending, plan, subscription
):
    pp = _client(
        capture_order=PayPalError("dup", status=422, issue="DUPLICATE_INVOICE_ID")
    )
    client.force_login(owner)
    with patch(CLIENT, return_value=pp):
        _return(client, pending)
        resp = client.get("/billing/checkout/?plan=starter&period=monthly")
    pending.refresh_from_db()
    assert pending.resolution == BillingPayment.UNRESOLVED
    assert pending.status == BillingPayment.PENDING
    assert resp["Location"] == "/billing/plans/"
    pp.create_order.assert_not_called()


def test_client_is_reused_for_the_same_credentials(settings):
    from apps.billing.paypal import get_paypal_client

    settings.PAYPAL_ENV = "sandbox"
    settings.PAYPAL_CLIENT_ID = "reuse-id"
    settings.PAYPAL_CLIENT_SECRET = "s1"
    first = get_paypal_client()
    assert get_paypal_client() is first
    settings.PAYPAL_CLIENT_SECRET = "s2"
    assert get_paypal_client() is not first
