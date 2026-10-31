"""PayPal Subscriptions: PayPal runs the renewal schedule, Akilent grants each period from a
completed payment.

``FakePayPal`` stands in for the REST client. Its sales are the recorded sandbox sale
(docs/billing/paypal/fixtures/sandbox/subscriptions/sale_v1.json) with ids and amounts swapped,
so the code reads the v1 shape PayPal really sends (``state``, ``amount.total``, ``custom``,
``billing_agreement_id``)."""

import copy
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.billing import payments, recurring, renewals
from apps.billing.models import (
    BillingPayment,
    BillingRefund,
    PayPalPlan,
    Subscription,
)
from apps.billing.paypal import PayPalError
from apps.billing.providers import registry
from apps.billing.tests.test_paypal import (
    CLIENT,
    FIXTURES,
    PAYPAL_SETTINGS,
    _event,
    _post,
)
from apps.core.models import SiteSettings

SALE = json.loads(
    (FIXTURES / "subscriptions" / "sale_v1.json").read_text(encoding="utf-8")
)["body"]


class FakePayPal:
    """Plans, subscriptions and sales as PayPal keeps them, for one merchant account."""

    def __init__(self):
        self.plans = {}
        self.subs = {}
        self.sales = {}
        self.refunds = {}
        self.cancelled = []
        self.cancel_error = None
        self.created = 0

    # client API
    def create_product(self, body, *, request_id):
        return {"id": "PROD-1"}

    def create_plan(self, body, *, request_id):
        self.created += 1
        plan_id = f"P-{self.created}"
        self.plans[plan_id] = body
        return {"id": plan_id, "status": "ACTIVE"}

    def create_subscription(self, body, *, request_id):
        sub_id = f"I-{len(self.subs) + 1}"
        self.subs[sub_id] = {
            "id": sub_id,
            "plan_id": body["plan_id"],
            "custom_id": body["custom_id"],
            "status": "APPROVAL_PENDING",
            "body": body,
        }
        return {
            "id": sub_id,
            "status": "APPROVAL_PENDING",
            "links": [
                {
                    "rel": "approve",
                    "href": f"https://www.sandbox.paypal.com/webapps/billing/subscriptions?ba_token=BA-{sub_id}",
                }
            ],
        }

    def get_subscription(self, sub_id):
        data = self.subs[sub_id]
        return {k: v for k, v in data.items() if k != "body"}

    def cancel_subscription(self, sub_id, reason):
        if self.cancel_error:
            raise self.cancel_error
        self.subs[sub_id]["status"] = "CANCELLED"
        self.cancelled.append(sub_id)

    def subscription_transactions(self, sub_id, start, end):
        if self.subs[sub_id]["status"] == "APPROVAL_PENDING":
            raise PayPalError("not found", status=404, issue="INVALID_RESOURCE_ID")
        return [
            {"id": s["id"], "status": s["state"].upper(), "time": s["create_time"]}
            for s in self.sales.values()
            if s["billing_agreement_id"] == sub_id
        ]

    def get_sale(self, sale_id):
        return copy.deepcopy(self.sales[sale_id])

    def get_sale_refund(self, refund_id):
        return self.refunds[refund_id]

    def verify_webhook_signature(self, fields, raw):
        return True

    # what happens at PayPal
    def approve(self, sub_id):
        self.subs[sub_id]["status"] = "ACTIVE"

    def charge(self, sub_id, *, state="pending", total=None, custom=None):
        sub = self.subs[sub_id]
        sale = copy.deepcopy(SALE)
        sale["id"] = f"SALE{len(self.sales) + 1}"
        sale["state"] = state
        sale["billing_agreement_id"] = sub_id
        sale["custom"] = custom or sub["custom_id"]
        sale["create_time"] = f"2026-10-{24 + len(self.sales):02d}T10:00:00Z"
        sale["amount"]["total"] = total or self._first_price(sub)
        if state != "pending":
            sale.pop("reason_code", None)
        self.sales[sale["id"]] = sale
        return sale["id"]

    def _first_price(self, sub):
        override = sub["body"].get("plan")
        if override and not any(
            s["billing_agreement_id"] == sub["id"] for s in self.sales.values()
        ):
            return override["billing_cycles"][0]["pricing_scheme"]["fixed_price"][
                "value"
            ]
        cycles = self.plans[sub["plan_id"]]["billing_cycles"]
        return cycles[-1]["pricing_scheme"]["fixed_price"]["value"]


@pytest.fixture
def fake():
    return FakePayPal()


@pytest.fixture
def paypal(db, settings, fake):
    for key, value in PAYPAL_SETTINGS.items():
        setattr(settings, key, value)
    settings.PAYPAL_SUBSCRIPTIONS = True
    registry.reset()
    site = SiteSettings.load()
    site.billing_provider = "paypal"
    site.payments_enabled = True
    site.save()
    with patch(CLIENT, return_value=fake):
        yield registry.get("paypal")
    registry.reset()


@pytest.fixture
def owner(account):
    from django.contrib.auth.models import User

    from apps.accounts.models import Membership

    user = User.objects.create_user(username="pps-owner", password="pw12345!")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


def _checkout(http, owner, method="", period="monthly"):
    http.force_login(owner)
    url = f"/billing/checkout/?plan=starter&period={period}"
    if method:
        url += f"&method={method}"
    return http.get(url)


def _subscribed(http, owner, fake, *, state="pending"):
    """Checkout, approve at PayPal, first payment taken in ``state``, customer returns."""
    _checkout(http, owner)
    payment = BillingPayment.objects.get(provider="paypal")
    sub_id = payment.provider_operation_id
    fake.approve(sub_id)
    sale_id = fake.charge(sub_id, state=state)
    http.get(
        f"/billing/return/paypal/?ref={payment.reference}&subscription_id={sub_id}"
        f"&ba_token=BA-x&token=T-x"
    )
    payment.refresh_from_db()
    return payment, sub_id, sale_id


def _sale_event(sale_id, kind="PAYMENT.SALE.COMPLETED", event_id=None):
    return _event(kind, {"id": sale_id}, event_id=event_id or f"WH-{sale_id}-{kind}")


# --- checkout ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_subscribing_is_the_only_way_to_pay(paypal):
    from apps.billing.views import checkout_options

    assert checkout_options() == [{"code": "paypal", "name": "Subscribe with PayPal"}]


@pytest.mark.django_db
def test_create_account_and_pay_goes_straight_to_the_paypal_subscription(
    client, owner, account, plan, paypal
):
    """Straight after a paid signup, the pay page starts the PayPal subscription by itself:
    there is one way to pay, so nobody has to pick it."""
    from apps.billing.views import PAY_AUTOSTART

    Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.INCOMPLETE,
        current_period_start=timezone.now(),
    )
    client.force_login(owner)
    session = client.session
    session[PAY_AUTOSTART] = True
    session.save()

    html = client.get("/billing/pay/").content.decode()
    assert "Pay once with debit or credit card" not in html
    assert "location.assign" in html
    assert (
        "/billing/checkout/?plan=starter&amp;period=monthly&amp;method=paypal" in html
    )


@pytest.mark.django_db
def test_checkout_creates_a_plan_once_and_a_subscription(
    client, owner, plan, subscription, paypal, fake
):
    resp = _checkout(client, owner)
    payment = BillingPayment.objects.get(provider="paypal")
    sub_id = payment.provider_operation_id
    assert resp.status_code == 302
    assert resp["Location"].endswith(f"ba_token=BA-{sub_id}")
    body = fake.subs[sub_id]["body"]
    assert body["custom_id"] == payment.reference
    assert "plan" not in body  # first payment is the full price: no override
    cycles = fake.plans[body["plan_id"]]["billing_cycles"]
    assert [(c["tenure_type"], c["total_cycles"]) for c in cycles] == [
        ("TRIAL", 1),
        ("REGULAR", 0),
    ]
    assert cycles[1]["frequency"] == {"interval_unit": "DAY", "interval_count": 30}
    assert cycles[1]["pricing_scheme"]["fixed_price"] == {
        "value": "19.00",
        "currency_code": "USD",
    }
    assert payment.raw["kind"] == "subscription"
    assert PayPalPlan.objects.get().paypal_plan_id == body["plan_id"]

    payments.abandon_open_checkouts(payment.account)
    _checkout(client, owner)
    assert fake.created == 1  # the PayPal plan is reused


@pytest.mark.django_db
def test_credit_lowers_only_the_first_payment(
    client, owner, account, plan, subscription, paypal, fake
):
    from apps.billing import credit

    credit.grant(account, Decimal("5.00"))
    _checkout(client, owner)
    payment = BillingPayment.objects.get(provider="paypal")
    assert payment.amount_due == Decimal("14.00")
    override = fake.subs[payment.provider_operation_id]["body"]["plan"]
    assert override["billing_cycles"] == [
        {
            "sequence": 1,
            "total_cycles": 1,
            "pricing_scheme": {
                "fixed_price": {"value": "14.00", "currency_code": "USD"}
            },
        }
    ]


@pytest.mark.django_db
def test_the_card_button_still_pays_once(client, owner, plan, subscription, paypal):
    order = {
        "id": "ORDER9",
        "links": [{"rel": "payer-action", "href": "https://pp/checkoutnow?token=O"}],
    }
    with patch.object(
        FakePayPal, "create_order", create=True, return_value=order
    ) as co:
        _checkout(client, owner, method="paypal-card")
    experience = co.call_args.args[0]["payment_source"]["paypal"]["experience_context"]
    assert experience["landing_page"] == "GUEST_CHECKOUT"
    assert BillingPayment.objects.get().provider_operation_id == "ORDER9"


# --- the first payment ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_held_first_payment_grants_nothing_and_stays_in_flight(
    client, owner, plan, subscription, paypal, fake
):
    payment, sub_id, _ = _subscribed(client, owner, fake, state="pending")
    subscription.refresh_from_db()
    assert payment.status == BillingPayment.PENDING
    assert payment.charge_started_at is not None
    assert subscription.status == Subscription.PAST_DUE
    assert payments.has_unconfirmed_charge(payment.account)

    # ACTIVATED arrives while the payment is under review: still nothing granted.
    assert (
        _post(
            client, _event("BILLING.SUBSCRIPTION.ACTIVATED", {"id": sub_id})
        ).status_code
        == 200
    )
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING

    # A day later it isn't abandoned: it is looked up and still pending.
    BillingPayment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(days=2),
        charge_started_at=timezone.now() - timedelta(days=2),
    )
    payments.abandon_stale()
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING


@pytest.mark.django_db
def test_the_first_payment_completing_activates_and_links_the_subscription(
    client, owner, plan, subscription, paypal, fake
):
    payment, sub_id, sale_id = _subscribed(client, owner, fake, state="pending")
    fake.sales[sale_id]["state"] = "completed"
    assert _post(client, _sale_event(sale_id)).status_code == 200

    payment.refresh_from_db()
    subscription.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    assert payment.provider_ref == sale_id
    assert subscription.status == Subscription.ACTIVE
    assert subscription.recurring_provider == "paypal"
    assert subscription.recurring_id == sub_id


@pytest.mark.django_db
def test_activated_finds_a_completed_first_payment_whose_webhook_was_lost(
    client, owner, plan, subscription, paypal, fake
):
    _checkout(client, owner)
    payment = BillingPayment.objects.get(provider="paypal")
    sub_id = payment.provider_operation_id
    fake.approve(sub_id)
    fake.charge(sub_id, state="completed")
    _post(client, _event("BILLING.SUBSCRIPTION.ACTIVATED", {"id": sub_id}))
    payment.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED


@pytest.mark.django_db
def test_a_sale_that_isnt_this_subscriptions_payment_grants_nothing(
    client, owner, plan, subscription, paypal, fake
):
    payment, sub_id, _ = _subscribed(client, owner, fake, state="pending")
    stranger = fake.charge(sub_id, state="completed", custom="pay_someone_else")
    _post(client, _sale_event(stranger))
    short = fake.charge(sub_id, state="completed", total="1.00")
    BillingPayment.objects.filter(pk=payment.pk).update(provider_ref="")
    _post(client, _sale_event(short))
    payment.refresh_from_db()
    subscription.refresh_from_db()
    assert payment.status == BillingPayment.FAILED  # paid the wrong amount
    assert subscription.status == Subscription.PAST_DUE


@pytest.mark.django_db
def test_return_before_approval_shows_the_pending_page(
    client, owner, plan, subscription, paypal, fake
):
    _checkout(client, owner)
    payment = BillingPayment.objects.get(provider="paypal")
    resp = client.get(
        f"/billing/return/paypal/?ref={payment.reference}"
        f"&subscription_id={payment.provider_operation_id}"
    )
    assert resp.status_code == 200
    payment.refresh_from_db()
    assert payment.charge_started_at is None  # nothing approved, nothing in flight


# --- renewals ------------------------------------------------------------------------------------


@pytest.fixture
def active(client, owner, plan, subscription, paypal, fake):
    payment, sub_id, sale_id = _subscribed(client, owner, fake, state="completed")
    subscription.refresh_from_db()
    assert subscription.recurring_id == sub_id
    return subscription, payment, sub_id


@pytest.mark.django_db
def test_each_renewal_paypal_takes_extends_the_plan_once(
    client, active, fake, django_capture_on_commit_callbacks
):
    subscription, first, sub_id = active
    end = subscription.current_period_end
    sale_id = fake.charge(sub_id, state="completed")
    with patch("apps.billing.tasks.send_billing_notice.delay") as notice:
        with django_capture_on_commit_callbacks(execute=True):
            assert _post(client, _sale_event(sale_id)).status_code == 200
        # PayPal resends it under another event id: still one renewal.
        _post(client, _sale_event(sale_id, event_id="WH-again"))

    renewal = BillingPayment.objects.get(provider_ref=sale_id)
    subscription.refresh_from_db()
    assert renewal.purpose == BillingPayment.RENEWAL
    assert renewal.status == BillingPayment.SUCCEEDED
    assert renewal.amount_due == Decimal("19.00")
    assert renewal.credit_applied == 0
    assert subscription.current_period_end == end + timedelta(days=30)
    assert BillingPayment.objects.filter(purpose=BillingPayment.RENEWAL).count() == 1
    notice.assert_called_once_with(subscription.account_id, "renewed", renewal.pk)


@pytest.mark.django_db
def test_the_renewal_engine_waits_for_paypal_then_counts_it_unpaid(active, fake):
    subscription, _, sub_id = active
    end = subscription.current_period_end
    assert renewals.renew(subscription.pk, now=end + timedelta(hours=1)) == ""
    assert not BillingPayment.objects.filter(purpose=BillingPayment.RENEWAL).exists()
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE

    later = end + renewals.RECURRING_GRACE + timedelta(hours=1)
    assert renewals.renew(subscription.pk, now=later) == "past_due"
    subscription.refresh_from_db()
    assert subscription.status == Subscription.PAST_DUE
    assert (
        subscription.recurring_id == sub_id
    )  # PayPal keeps retrying; a late payment counts


@pytest.mark.django_db
def test_a_renewal_under_review_is_not_counted_unpaid(client, active, fake):
    subscription, _, sub_id = active
    _post(
        client,
        _sale_event(fake.charge(sub_id, state="pending"), "PAYMENT.SALE.PENDING"),
    )
    later = (
        subscription.current_period_end + renewals.RECURRING_GRACE + timedelta(days=1)
    )
    assert renewals.renew(subscription.pk, now=later) == ""
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE


@pytest.mark.django_db
def test_expiry_stops_the_paypal_subscription(
    active, fake, django_capture_on_commit_callbacks
):
    subscription, _, sub_id = active
    Subscription.objects.filter(pk=subscription.pk).update(
        status=Subscription.PAST_DUE,
        past_due_since=timezone.now() - timedelta(days=30),
        current_period_end=timezone.now() - timedelta(days=30),
    )
    with django_capture_on_commit_callbacks(execute=True):
        assert renewals.renew(subscription.pk) == "expired"
    subscription.refresh_from_db()
    assert fake.cancelled == [sub_id]
    assert subscription.recurring_id == ""
    assert subscription.recurring_stop_id == ""


@pytest.mark.django_db
def test_sync_records_a_renewal_whose_webhook_was_lost(active, fake):
    subscription, _, sub_id = active
    sale_id = fake.charge(sub_id, state="completed")
    recurring.sync_all()
    assert BillingPayment.objects.get(provider_ref=sale_id).status == (
        BillingPayment.SUCCEEDED
    )


# --- cancelling and plan changes -----------------------------------------------------------------


@pytest.mark.django_db
def test_cancel_stops_paypal_before_confirming(client, owner, active, fake):
    subscription, _, sub_id = active
    fake.cancel_error = PayPalError("down", status=503)
    client.post("/billing/cancel/")
    subscription.refresh_from_db()
    assert subscription.recurring_id == sub_id
    assert not subscription.cancel_at_period_end

    fake.cancel_error = None
    client.post("/billing/cancel/")
    subscription.refresh_from_db()
    assert fake.cancelled == [sub_id]
    assert subscription.recurring_id == ""
    assert subscription.cancel_at_period_end


@pytest.mark.django_db
def test_cancelling_in_paypal_ends_the_plan_with_the_paid_period(client, active, fake):
    subscription, _, sub_id = active
    fake.subs[sub_id]["status"] = "CANCELLED"
    _post(client, _event("BILLING.SUBSCRIPTION.CANCELLED", {"id": sub_id}))
    subscription.refresh_from_db()
    assert subscription.recurring_id == ""
    assert subscription.cancel_at_period_end
    assert subscription.status == Subscription.ACTIVE


@pytest.mark.django_db
def test_a_new_subscription_replaces_the_old_one_once_it_is_paid(
    client, owner, active, fake, django_capture_on_commit_callbacks
):
    subscription, _, old_id = active
    _checkout(client, owner, period="annually")
    new = BillingPayment.objects.filter(purpose=BillingPayment.CHECKOUT).latest("pk")
    new_id = new.provider_operation_id
    fake.approve(new_id)
    sale_id = fake.charge(new_id, state="pending")
    _post(client, _sale_event(sale_id, "PAYMENT.SALE.PENDING"))
    assert fake.cancelled == []  # not paid yet: the old one keeps the plan renewing

    fake.sales[sale_id]["state"] = "completed"
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, _sale_event(sale_id))
    subscription.refresh_from_db()
    assert subscription.recurring_id == new_id
    assert fake.cancelled == [old_id]
    assert subscription.recurring_stop_id == ""


@pytest.mark.django_db
def test_a_stop_that_fails_is_retried_by_the_daily_sync(
    client, owner, active, fake, django_capture_on_commit_callbacks
):
    subscription, _, old_id = active
    fake.cancel_error = PayPalError("down", status=503)
    _checkout(client, owner, period="annually")
    new = BillingPayment.objects.filter(purpose=BillingPayment.CHECKOUT).latest("pk")
    fake.approve(new.provider_operation_id)
    sale_id = fake.charge(new.provider_operation_id, state="completed")
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, _sale_event(sale_id))
    subscription.refresh_from_db()
    assert subscription.recurring_stop_id == f"paypal:{old_id}"

    fake.cancel_error = None
    recurring.sync_all()
    subscription.refresh_from_db()
    assert fake.cancelled == [old_id]
    assert subscription.recurring_stop_id == ""


@pytest.mark.django_db
def test_money_taken_by_a_replaced_subscription_is_never_granted(
    client, active, fake, django_capture_on_commit_callbacks
):
    subscription, first, sub_id = active
    Subscription.objects.filter(pk=subscription.pk).update(recurring_id="I-OTHER")
    sale_id = fake.charge(sub_id, state="completed")
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, _sale_event(sale_id))
    assert not BillingPayment.objects.filter(provider_ref=sale_id).exists()
    assert fake.cancelled == [sub_id]
    from apps.core.models import AdminAction

    assert AdminAction.objects.filter(action="billing.orphan_charge").exists()


# --- refunds -------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_refunded_subscription_payment_is_recorded(client, active, fake):
    subscription, first, _ = active
    fake.refunds["RF1"] = {
        "id": "RF1",
        "state": "completed",
        "sale_id": first.provider_ref,
        "amount": {"total": "5.00", "currency": "USD"},
    }
    _post(client, _event("PAYMENT.SALE.REFUNDED", {"id": "RF1"}))
    first.refresh_from_db()
    assert BillingRefund.objects.get().amount == Decimal("5.00")
    assert first.status == BillingPayment.SUCCEEDED
    assert first.refund_state == BillingPayment.REFUND_PARTIAL


# --- what the plans page says --------------------------------------------------------------------


@pytest.mark.django_db
def test_plans_page_says_how_the_plan_really_renews(active, fake, settings):
    from apps.billing.views import _renewal_note

    subscription, _, _ = active
    assert _renewal_note(subscription.account, subscription) == (
        "Renews automatically through PayPal."
    )
    subscription.recurring_id = ""
    assert "email you a link" in _renewal_note(subscription.account, subscription)


# --- review round 4 ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_renewal_completing_after_a_plan_change_is_not_granted(
    client, owner, active, fake, django_capture_on_commit_callbacks
):
    """A renewal on the old subscription, still pending when the customer switched, completes
    later: it must not grant the old plan, re-link the old subscription or stop the new one."""
    from apps.core.models import AdminAction

    subscription, _, old_id = active
    old_sale = fake.charge(old_id, state="pending")
    _post(client, _sale_event(old_sale, "PAYMENT.SALE.PENDING"))
    renewal = BillingPayment.objects.get(provider_ref=old_sale)
    BillingPayment.objects.filter(pk=renewal.pk).update(
        status=BillingPayment.SUCCEEDED
    )  # not in flight, so the switch below isn't blocked
    _checkout(client, owner, period="annually")
    BillingPayment.objects.filter(pk=renewal.pk).update(status=BillingPayment.PENDING)
    new = BillingPayment.objects.filter(purpose=BillingPayment.CHECKOUT).latest("pk")
    fake.approve(new.provider_operation_id)
    with django_capture_on_commit_callbacks(execute=True):
        _post(
            client,
            _sale_event(fake.charge(new.provider_operation_id, state="completed")),
        )
    subscription.refresh_from_db()
    assert subscription.billing_period == Subscription.ANNUALLY

    fake.sales[old_sale]["state"] = "completed"
    with django_capture_on_commit_callbacks(execute=True):
        _post(client, _sale_event(old_sale))
        _post(client, _sale_event(old_sale, event_id="WH-again"))
    subscription.refresh_from_db()
    renewal.refresh_from_db()
    assert subscription.recurring_id == new.provider_operation_id
    assert subscription.billing_period == Subscription.ANNUALLY
    assert renewal.status == BillingPayment.ABANDONED
    assert fake.cancelled == [old_id]
    assert AdminAction.objects.filter(action="billing.orphan_charge").count() == 1


@pytest.mark.django_db
def test_a_missed_paypal_renewal_emails_no_pay_link_and_blocks_paying_twice(
    client, owner, active, fake
):
    from apps.billing.tasks import _notice

    subscription, _, _ = active
    subject, body = _notice(subscription, "unpaid", None)
    assert "/billing/checkout/" not in body
    assert "try again automatically" in body
    resp = _checkout(client, owner)
    assert resp["Location"] == "/billing/plans/"
    assert BillingPayment.objects.filter(purpose=BillingPayment.CHECKOUT).count() == 1


@pytest.mark.django_db
def test_a_declined_first_payment_that_paypal_retries_still_activates(
    client, owner, plan, subscription, paypal, fake
):
    _checkout(client, owner)
    payment = BillingPayment.objects.get(provider="paypal")
    sub_id = payment.provider_operation_id
    fake.approve(sub_id)
    denied = fake.charge(sub_id, state="denied")
    _post(client, _sale_event(denied, "PAYMENT.SALE.DENIED"))
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING  # PayPal retries; still in flight
    assert payment.charge_started_at is not None

    fake.charge(sub_id, state="completed")  # the retry, whose webhook is lost
    from apps.billing.providers import registry as reg

    result = reg.get("paypal").reconcile(payment)
    payments.record_result("paypal", result)
    payment.refresh_from_db()
    subscription.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    assert subscription.recurring_id == sub_id


@pytest.mark.django_db
def test_a_declined_first_payment_fails_once_paypal_gives_up(
    client, owner, plan, subscription, paypal, fake
):
    _checkout(client, owner)
    payment = BillingPayment.objects.get(provider="paypal")
    sub_id = payment.provider_operation_id
    fake.approve(sub_id)
    denied = fake.charge(sub_id, state="denied")
    fake.subs[sub_id]["status"] = "SUSPENDED"
    _post(client, _sale_event(denied, "PAYMENT.SALE.DENIED"))
    payment.refresh_from_db()
    assert payment.status == BillingPayment.FAILED


@pytest.mark.django_db
def test_the_stop_list_has_room_for_many(active):
    subscription, _, _ = active
    field = Subscription._meta.get_field("recurring_stop_id")
    assert field.get_internal_type() == "TextField"


@pytest.mark.django_db
def test_an_account_without_a_subscription_row_is_still_linked(
    client, owner, plan, account, paypal, fake
):
    Subscription.objects.filter(account=account).delete()
    _checkout(client, owner)
    payment = BillingPayment.objects.get(provider="paypal")
    fake.approve(payment.provider_operation_id)
    _post(
        client,
        _sale_event(fake.charge(payment.provider_operation_id, state="completed")),
    )
    sub = Subscription.objects.get(account=account)
    assert sub.status == Subscription.ACTIVE
    assert sub.recurring_id == payment.provider_operation_id


@pytest.mark.django_db
def test_cancelling_while_the_first_payment_settles_stops_it_and_stays_cancelled(
    client, owner, plan, subscription, paypal, fake
):
    payment, sub_id, sale_id = _subscribed(client, owner, fake, state="pending")
    Subscription.objects.filter(pk=subscription.pk).update(
        status=Subscription.ACTIVE,
        current_period_end=timezone.now() + timedelta(days=3),
    )
    client.post("/billing/cancel/")
    assert fake.cancelled == [sub_id]

    fake.sales[sale_id]["state"] = "completed"
    _post(client, _sale_event(sale_id))
    payment.refresh_from_db()
    subscription.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED  # paid, so the period is granted
    assert subscription.cancel_at_period_end  # but the cancel stands
    assert subscription.recurring_id == ""


@pytest.mark.django_db
def test_the_renewal_notice_quotes_the_price_subscribed_at(active, plan):
    from apps.billing.tasks import _notice

    subscription, _, _ = active
    plan.price_monthly = Decimal("29.00")
    plan.save()
    subscription.refresh_from_db()
    _, body = _notice(subscription, "upcoming", None)
    assert "19.00" in body
    assert "29.00" not in body


@pytest.mark.django_db
def test_resume_says_automatic_payments_stopped_only_once(client, owner, active, fake):
    subscription, _, _ = active
    client.post("/billing/cancel/")
    client.post("/billing/resume/")
    subscription.refresh_from_db()
    assert (
        subscription.recurring_provider == ""
    )  # the next cancel/resume is a plain one
    assert not recurring.stopped_automatic_payments(subscription)
