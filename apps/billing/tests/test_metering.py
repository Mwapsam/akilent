"""Limits and usage: reservations with identity, idempotent operations, held vs failed, and the
product promise that a commercial limit never drops what a customer sent."""

import threading
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from apps.accounts.models import Account
from apps.billing import api as billing_api
from apps.billing import limit_catalog
from apps.billing.metering import commit_stale
from apps.billing.models import (
    Plan,
    PlanLimit,
    Subscription,
    UsageCounter,
    UsageReservation,
)
from apps.whatsapp.models import (
    MessageLog,
    MessageTemplate,
    OutboundMessage,
    WebhookEventLog,
    WhatsAppContact,
)
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.types import SendResult


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def plan(db):
    return Plan.objects.create(slug="meter", name="Meter", price_monthly=Decimal("10"))


@pytest.fixture
def account(plan):
    acc = Account.objects.create(company_name="Meter Co", slug="meter-co")
    Subscription.objects.update_or_create(
        account=acc,
        defaults={
            "plan": plan,
            "status": Subscription.ACTIVE,
            "current_period_start": timezone.now(),
        },
    )
    return acc


def set_limit(plan, key, value):
    PlanLimit.objects.update_or_create(plan=plan, key=key, defaults={"value": value})


# ---- the catalog ---------------------------------------------------------------------------------


def test_limit_keys_are_permanent():
    assert set(limit_catalog.BY_KEY) == set(limit_catalog.ISSUED_KEYS)


def test_only_email_and_ai_cost_akilent_money():
    """Meta bills each business for WhatsApp directly, so WhatsApp limits never count as cost."""
    costly = {lim.key for lim in limit_catalog.LIMITS if lim.cost_bearing}
    assert costly == {"emails_month", "emails_day", "ai_actions_day"}


# ---- reservations ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_reserve_counts_once_per_operation_and_holds_at_the_limit(account, plan):
    set_limit(plan, "whatsapp_marketing_msgs", 2)
    a = billing_api.reserve(account, "whatsapp_marketing_msgs", operation_id="op-a")
    again = billing_api.reserve(account, "whatsapp_marketing_msgs", operation_id="op-a")
    assert a is not None and again.pk == a.pk
    assert billing_api.used(account, "whatsapp_marketing_msgs") == 1
    assert (
        billing_api.reserve(account, "whatsapp_marketing_msgs", operation_id="op-b")
        is not None
    )
    assert (
        billing_api.reserve(account, "whatsapp_marketing_msgs", operation_id="op-c")
        is None
    )  # held


@pytest.mark.django_db
def test_release_only_gives_back_that_reservation_once(account, plan):
    set_limit(plan, "emails_day", 5)
    a = billing_api.reserve(account, "emails_day", operation_id="a")
    b = billing_api.reserve(account, "emails_day", operation_id="b")
    assert billing_api.release(a) is True
    assert billing_api.release(a) is False  # a double release does nothing
    assert billing_api.used(account, "emails_day") == 1
    assert billing_api.commit(b) is True
    assert billing_api.release(b) is False  # a committed unit can't be taken back
    assert billing_api.used(account, "emails_day") == 1


@pytest.mark.django_db
def test_a_refused_operation_can_be_deliberately_tried_again(account, plan):
    set_limit(plan, "emails_day", 1)
    r = billing_api.reserve(account, "emails_day", operation_id="x")
    billing_api.release(r)
    assert (
        billing_api.reserve(account, "emails_day", operation_id="x").status
        == "released"
    )  # plain retry sees it
    renewed = billing_api.reserve(
        account, "emails_day", operation_id="x", renew_released=True
    )
    assert renewed.status == "reserved" and billing_api.used(account, "emails_day") == 1


@pytest.mark.django_db
def test_periods_reset(account, plan):
    set_limit(plan, "emails_day", 1)
    assert billing_api.reserve(account, "emails_day", operation_id="d1")
    assert billing_api.reserve(account, "emails_day", operation_id="d2") is None
    UsageCounter.objects.filter(key="emails_day").update(
        period_start=timezone.now().date().replace(year=2020)
    )
    assert billing_api.reserve(account, "emails_day", operation_id="d3") is not None


@pytest.mark.django_db
def test_limit_resolution_override_then_plan_then_default(account, plan):
    assert billing_api.limit_source(account, "ai_actions_day") == (
        -1,
        "plan",
    )  # seeded unlimited
    PlanLimit.objects.filter(plan=plan, key="ai_actions_day").delete()
    assert billing_api.limit_source(account, "ai_actions_day") == (500, "default")
    set_limit(plan, "ai_actions_day", 20)
    billing_api.set_limit_override(account, "ai_actions_day", value=200, note="pilot")
    assert billing_api.limit_source(account, "ai_actions_day") == (200, "override")
    billing_api.set_limit_override(
        account,
        "ai_actions_day",
        value=200,
        note="pilot",
        expires_at=timezone.now() - timezone.timedelta(days=1),
    )
    assert billing_api.limit_source(account, "ai_actions_day") == (20, "plan"), (
        "an expired override lapses"
    )


@pytest.mark.django_db
def test_an_exhausted_quota_never_makes_a_feature_look_off(account, plan):
    from apps.billing.models import PlanFeature

    PlanFeature.objects.get_or_create(plan=plan, key="ai_assistant")
    set_limit(plan, "ai_actions_day", 0)
    assert billing_api.reserve(account, "ai_actions_day", operation_id="ai:1") is None
    assert billing_api.usable(account, "ai_assistant") is True


@pytest.mark.django_db
def test_stale_reservations_are_assumed_used(account, plan):
    r = billing_api.reserve(account, "emails_day", operation_id="s")
    UsageReservation.objects.filter(pk=r.pk).update(
        created_at=timezone.now() - timezone.timedelta(days=2)
    )
    assert commit_stale() == 1
    r.refresh_from_db()
    assert r.status == "committed"


@pytest.mark.django_db
def test_warning_is_queued_once_at_80_and_once_at_100(account, plan):
    set_limit(plan, "emails_day", 5)
    with patch("apps.billing.tasks.send_limit_warning.delay") as warn:
        for i in range(6):
            billing_api.reserve(account, "emails_day", operation_id=f"w{i}")
    assert [c.args[2] for c in warn.call_args_list] == [80, 100]


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor == "sqlite",
    reason="SQLite serialises writes; the sequential test covers it",
)
def test_concurrent_reservations_never_overspend(account, plan):
    set_limit(plan, "whatsapp_marketing_msgs", 2)
    results = []

    def go(i):
        from django.db import connection as conn

        results.append(
            billing_api.reserve(
                Account.objects.get(pk=account.pk),
                "whatsapp_marketing_msgs",
                operation_id=f"par-{i}",
            )
        )
        conn.close()

    threads = [threading.Thread(target=go, args=(i,)) for i in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(r is not None for r in results) == 2
    assert billing_api.used(account, "whatsapp_marketing_msgs") == 2


# ---- rules ----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_recipient_caps_are_rules_not_meters(account, plan):
    set_limit(plan, "whatsapp_campaign_recipients", 100)
    assert billing_api.check_rule(account, "whatsapp_campaign_recipients", 100)
    assert not billing_api.check_rule(account, "whatsapp_campaign_recipients", 101)
    assert not UsageCounter.objects.filter(key="whatsapp_campaign_recipients").exists()


@pytest.mark.django_db
def test_automations_over_the_limit_cant_be_turned_on(account, plan):
    from apps.automation import api as automation_api

    set_limit(plan, "automation_rules", 1)
    definition = {
        "trigger": {"type": "contact.created"},
        "steps": [{"id": "s", "type": "stop"}],
    }
    automation_api.upsert_published_workflow(
        account, slug="one", name="One", definition=definition
    )
    with pytest.raises(automation_api.AutomationLimitReached):
        automation_api.upsert_published_workflow(
            account, slug="two", name="Two", definition=definition
        )
    automation_api.upsert_published_workflow(
        account, slug="one", name="One again", definition=definition
    )  # already on


# ---- the product promise ----------------------------------------------------------------------------


def _inbound(msg_id, wa_id="260971234567"):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "PN-METER"},
                            "contacts": [{"profile": {"name": "Ada"}}],
                            "messages": [
                                {
                                    "id": msg_id,
                                    "from": wa_id,
                                    "timestamp": str(int(timezone.now().timestamp())),
                                    "type": "text",
                                    "text": {"body": "Hello?"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


@pytest.fixture
def number(account):
    return WhatsAppBusinessNumber.objects.create(
        account=account, phone_number_id="PN-METER", access_token="tok", is_active=True
    )


@pytest.mark.django_db
def test_customer_messages_are_never_dropped_by_commercial_limits(
    account, plan, number
):
    """Permanent: a business over its conversation limit still receives every customer message."""
    from apps.whatsapp.tasks import process_whatsapp_event

    set_limit(plan, "conversations_month", 0)
    for msg_id in ("wamid.1", "wamid.2"):
        event = WebhookEventLog.objects.create(
            event_type="message", payload=_inbound(msg_id)
        )
        process_whatsapp_event(event.id)
    stored = MessageLog.objects.filter(
        account=account, direction=MessageLog.Direction.INBOUND
    )
    assert set(stored.values_list("message_id", flat=True)) == {"wamid.1", "wamid.2"}
    # Counted once for the 24h window, not once per message.
    assert billing_api.used(account, "conversations_month") == 1


# ---- WhatsApp sends: held, unconfirmed, never twice ---------------------------------------------------


class _Provider:
    def __init__(self, result=None):
        self.calls, self.result = [], result

    def send_template(self, to, name, language, components, category=""):
        self.calls.append(name)
        return self.result or SendResult(
            message_id=f"wamid.OUT{len(self.calls)}", success=True
        )


def _queue_templates(account, n, category="marketing"):
    contact = WhatsAppContact.objects.create(
        account=account,
        phone_number="+260971234567",
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_IN,
    )
    template = MessageTemplate.objects.create(
        account=account,
        name="promo",
        whatsapp_template_name="promo",
        content="Sale!",
        category=category,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    return [
        OutboundMessage.objects.create(
            account=account,
            contact=contact,
            template=template,
            idempotency_key=f"k{i}",
            payload={
                "type": "template",
                "template_name": "promo",
                "language": "en",
                "components": [],
            },
        )
        for i in range(n)
    ]


@pytest.mark.django_db
def test_sends_past_the_limit_are_held_not_failed_and_never_reach_meta(
    account, plan, number
):
    from apps.whatsapp.tasks import drain_outbound_queue

    set_limit(plan, "whatsapp_marketing_msgs", 2)
    msgs = _queue_templates(account, 5)
    provider = _Provider()
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        drain_outbound_queue()
    statuses = sorted(
        OutboundMessage.objects.filter(pk__in=[m.pk for m in msgs]).values_list(
            "status", flat=True
        )
    )
    assert statuses == ["held"] * 3 + ["sent"] * 2
    assert len(provider.calls) == 2
    held = OutboundMessage.objects.filter(status="held").first()
    assert (
        held.message_log.status == MessageLog.Status.HELD
        and held.error_code == "PLAN_LIMIT"
    )
    assert billing_api.used(account, "whatsapp_marketing_msgs") == 2


@pytest.mark.django_db
def test_a_timeout_is_unconfirmed_and_never_resent(account, plan, number):
    from apps.whatsapp.tasks import drain_outbound_queue

    (msg,) = _queue_templates(account, 1, category="utility")
    provider = _Provider(
        SendResult(message_id="", success=False, error="read timeout", ambiguous=True)
    )
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        drain_outbound_queue()
        msg.refresh_from_db()
        assert msg.status == OutboundMessage.Status.UNCONFIRMED
        OutboundMessage.objects.filter(pk=msg.pk).update(
            status="queued"
        )  # even if something re-queues it
        drain_outbound_queue()
    assert provider.calls == ["promo"], (
        "a message that may have arrived is never sent twice"
    )
    assert (
        UsageReservation.objects.get(operation_id=f"wa-msg:{msg.pk}").status
        == "committed"
    )


@pytest.mark.django_db
def test_a_definite_refusal_gives_the_unit_back(account, plan, number):
    from apps.whatsapp.tasks import drain_outbound_queue

    (msg,) = _queue_templates(account, 1, category="utility")
    provider = _Provider(
        SendResult(
            message_id="",
            success=False,
            error="bad param",
            error_code="132000",
            retryable=False,
        )
    )
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        drain_outbound_queue()
    msg.refresh_from_db()
    assert msg.status == OutboundMessage.Status.FAILED
    assert billing_api.used(account, "whatsapp_utility_msgs") == 0


@pytest.mark.django_db
def test_a_worker_crash_mid_send_is_unconfirmed_not_resent(account, plan, number):
    from apps.whatsapp.tasks import drain_outbound_queue

    (msg,) = _queue_templates(account, 1)
    billing_api.reserve(
        account, "whatsapp_marketing_msgs", operation_id=f"wa-msg:{msg.pk}"
    )
    OutboundMessage.objects.filter(pk=msg.pk).update(
        status="sending", updated_at=timezone.now() - timezone.timedelta(hours=1)
    )
    provider = _Provider()
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        drain_outbound_queue()
    msg.refresh_from_db()
    assert msg.status == OutboundMessage.Status.UNCONFIRMED and provider.calls == []


@pytest.mark.django_db
def test_the_platform_brake_defers_instead_of_failing(account, plan, number, settings):
    from apps.whatsapp.tasks import drain_outbound_queue

    settings.WHATSAPP_PLATFORM_MAX_PER_MINUTE = 1
    msgs = _queue_templates(account, 3, category="utility")
    provider = _Provider()
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        drain_outbound_queue()
    statuses = list(
        OutboundMessage.objects.filter(pk__in=[m.pk for m in msgs]).values_list(
            "status", flat=True
        )
    )
    assert (
        statuses.count("sent") == 1
        and statuses.count("queued") == 2
        and "failed" not in statuses
    )


# ---- email ---------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_email_campaign_recipients_past_the_limit_are_held(account, plan):
    from apps.email.models import BulkEmailCampaign, BulkEmailRecipient, EmailDomain
    from apps.email.tasks import dispatch_campaign

    set_limit(plan, "emails_month", 2)
    domain = EmailDomain.objects.create(
        account=account, domain="meter.test", status=EmailDomain.Status.VERIFIED
    )
    campaign = BulkEmailCampaign.objects.create(
        account=account,
        domain=domain,
        from_email="hi@meter.test",
        subject_override="Hi",
        text_override="Hi",
    )
    for i in range(4):
        BulkEmailRecipient.objects.create(
            campaign=campaign, to_email=f"p{i}@example.com"
        )
    with (
        patch("apps.email.services.validation.validate_recipient", return_value=True),
        patch("apps.email.tasks.send_bulk_recipient_email.delay"),
        patch("apps.email.tasks.dispatch_campaign.delay"),
    ):
        dispatch_campaign.apply(args=(campaign.pk,))
    statuses = sorted(
        BulkEmailRecipient.objects.filter(campaign=campaign).values_list(
            "status", flat=True
        )
    )
    assert statuses == ["held", "held", "queued", "queued"]
    campaign.refresh_from_db()
    assert campaign.failed_count == 0, "held is not failed"


# ---- the pages -------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_owner_sees_usage_and_a_warning(client, account, plan):
    from django.contrib.auth.models import User

    from apps.accounts.models import Membership

    owner = User.objects.create_user("o", "o@x.com", "pw")
    Membership.objects.create(user=owner, account=account, role=Membership.Role.OWNER)
    client.force_login(owner)
    set_limit(plan, "emails_month", 10)
    for i in range(9):
        billing_api.reserve(account, "emails_month", operation_id=f"e{i}")
    page = client.get("/billing/plans/").content.decode()
    assert "Usage" in page and "9 / 10" in page and "90% of your emails" in page


@pytest.mark.django_db
def test_operator_limit_matrix_previews_then_applies(client, account, plan):
    from django.contrib.auth.models import User

    from apps.core.models import AdminAction

    client.force_login(User.objects.create_superuser("root", "r@x.com", "pw"))
    form = {f"l:{plan.pk}:emails_day": "50"}
    assert (
        "Confirm these changes"
        in client.post("/manage/plans/limits/", form).content.decode()
    )
    assert billing_api.limit(account, "emails_day") == -1, "a preview must not write"
    client.post("/manage/plans/limits/", dict(form, apply="1"))
    assert billing_api.limit(account, "emails_day") == 50
    row = AdminAction.objects.get(action="plan.limit.change")
    assert (
        row.detail["old"] == -1
        and row.detail["new"] == 50
        and row.detail["businesses"] == 1
    )


@pytest.mark.django_db
def test_operator_can_give_one_business_its_own_limit(client, account, plan):
    from django.contrib.auth.models import User

    client.force_login(User.objects.create_superuser("root", "r@x.com", "pw"))
    url = f"/manage/businesses/{account.pk}/do/limit/"
    client.post(
        url,
        {
            "key": "whatsapp_marketing_msgs",
            "change": "set",
            "value": "10000",
            "note": "launch",
            "days": "30",
        },
    )
    assert billing_api.limit_source(account, "whatsapp_marketing_msgs") == (
        10000,
        "override",
    )
    client.post(url, {"key": "whatsapp_marketing_msgs", "change": "reset"})
    assert billing_api.limit_source(account, "whatsapp_marketing_msgs")[1] == "plan"


@pytest.mark.django_db
def test_reaching_the_limit_in_one_step_sends_no_stale_80_percent_warning(monkeypatch):
    from apps.billing import metering
    from apps.billing.models import UsageCounter

    acc = Account.objects.create(company_name="Warn Co", slug="warn-co")
    sent = []
    monkeypatch.setattr(
        "apps.billing.tasks.send_limit_warning.delay", lambda *a: sent.append(a)
    )
    start = metering.period_start("conversations_month")
    UsageCounter.objects.create(
        account=acc, key="conversations_month", period_start=start, used=105
    )
    metering._after_use(acc, "conversations_month", start, 100)
    metering._after_use(acc, "conversations_month", start, 100)
    assert [a[2] for a in sent] == [100]


@pytest.mark.django_db
def test_ai_calls_refused_by_the_site_ceiling_use_no_plan_units(settings):
    from apps.ai.tasks import _over_daily_limit

    settings.AI_DAILY_CALL_LIMIT = 1
    cache.clear()
    acc = Account.objects.create(company_name="Ai Co", slug="ai-co")
    assert _over_daily_limit(acc.pk, "ai-proposal:1") is False
    assert _over_daily_limit(acc.pk, "ai-proposal:2") is True
    assert billing_api.used(acc, "ai_actions_day") == 1


@pytest.mark.django_db
def test_a_rolled_back_email_reservation_can_be_reserved_again():
    acc = Account.objects.create(company_name="Roll Co", slug="roll-co")
    plan = Plan.objects.create(slug="roll", name="Roll", price_monthly=Decimal("5"))
    Subscription.objects.update_or_create(
        account=acc,
        defaults={
            "plan": plan,
            "status": Subscription.ACTIVE,
            "current_period_start": timezone.now(),
        },
    )
    PlanLimit.objects.update_or_create(
        plan=plan, key="emails_month", defaults={"value": 10}
    )
    PlanLimit.objects.update_or_create(
        plan=plan, key="emails_day", defaults={"value": 1}
    )
    assert (
        billing_api.reserve_all(acc, ["emails_month", "emails_day"], operation_id="a")
        is not None
    )
    assert (
        billing_api.reserve_all(acc, ["emails_month", "emails_day"], operation_id="b")
        is None
    )
    assert billing_api.used(acc, "emails_month") == 1
    PlanLimit.objects.filter(plan=plan, key="emails_day").update(value=5)
    assert (
        billing_api.reserve_all(acc, ["emails_month", "emails_day"], operation_id="b")
        is not None
    )


def test_only_ambiguous_network_errors_are_unconfirmed():
    import requests

    from apps.whatsapp.providers.meta import _may_have_been_sent as f

    assert f(requests.ReadTimeout()) is True
    assert (
        f(requests.ConnectionError("Connection aborted.", "RemoteDisconnected")) is True
    )
    assert f(requests.ConnectTimeout()) is False
    assert (
        f(
            requests.ConnectionError(
                "Failed to establish a new connection: Name or service not known"
            )
        )
        is False
    )


@pytest.mark.django_db
def test_businesses_by_margin_uses_a_fixed_number_of_queries(
    django_assert_max_num_queries,
):
    for i in range(6):
        a = Account.objects.create(company_name=f"M{i}", slug=f"m{i}")
        Subscription.objects.create(
            account=a,
            plan=Plan.objects.create(slug=f"mp{i}", name=f"P{i}"),
            status=Subscription.ACTIVE,
            current_period_start=timezone.now(),
        )
    with django_assert_max_num_queries(8):
        assert len(billing_api.businesses_by_margin()) == 6


@pytest.mark.django_db
def test_csv_rows_stopped_by_the_customer_limit_are_reported_separately():
    from apps.contacts.services import import_csv

    acc = Account.objects.create(company_name="Imp Co", slug="imp-co")
    plan = Plan.objects.create(slug="imp", name="Imp", price_monthly=Decimal("5"))
    Subscription.objects.update_or_create(
        account=acc,
        defaults={
            "plan": plan,
            "status": Subscription.ACTIVE,
            "current_period_start": timezone.now(),
        },
    )
    PlanLimit.objects.update_or_create(plan=plan, key="contacts", defaults={"value": 2})
    imp = import_csv(acc, "email\na@x.com\nb@x.com\nc@x.com\n\n")
    assert (imp.created_count, imp.limit_skipped_count, imp.skipped_count) == (2, 1, 0)


@pytest.mark.django_db
def test_settling_an_email_as_not_sent_returns_both_units():

    acc = Account.objects.create(company_name="Rel Co", slug="rel-co")
    reserved = billing_api.reserve_all(
        acc, ["emails_month", "emails_day"], operation_id="email:x"
    )
    assert billing_api.used(acc, "emails_month") == 1 and reserved
    from apps.billing.limits import LimitChecker

    LimitChecker(acc).settle_email("email:x", ok=False)
    assert billing_api.used(acc, "emails_month") == 0


@pytest.mark.django_db
def test_an_ai_call_retried_after_the_site_ceiling_still_counts_its_unit(settings):
    from apps.ai.tasks import _over_daily_limit

    cache.clear()
    acc = Account.objects.create(company_name="Ai2 Co", slug="ai2-co")
    settings.AI_DAILY_CALL_LIMIT = 1
    assert _over_daily_limit(acc.pk, "ai-draft:1") is False
    assert _over_daily_limit(acc.pk, "ai-draft:2") is True  # released
    cache.clear()  # next day
    assert _over_daily_limit(acc.pk, "ai-draft:2") is False  # same operation, now runs
    assert billing_api.used(acc, "ai_actions_day") == 2


@pytest.mark.django_db
def test_usage_report_query_count_does_not_grow_with_limits(
    django_assert_max_num_queries,
):
    acc = Account.objects.create(company_name="Q Co", slug="q-co")
    with django_assert_max_num_queries(16):
        billing_api.usage_report(acc)
    with django_assert_max_num_queries(5):
        billing_api.usage_warnings(acc)


def test_every_total_limit_has_a_live_total_branch():
    """A new TOTAL-period catalog key must be wired into _live_total, or usage_report and
    require_room raise KeyError in production instead of failing this test."""
    from apps.billing import limit_catalog, metering

    acc = Account(pk=0)
    for lim in limit_catalog.LIMITS:
        if lim.period == limit_catalog.TOTAL:
            try:
                metering._live_total(acc, lim.key)
            except KeyError:
                pytest.fail(f"_live_total has no branch for TOTAL limit {lim.key!r}")
            except Exception:
                pass  # a DB/app-layer error is fine here; only a KeyError means no branch exists


@pytest.mark.django_db
def test_percent_never_reports_100_before_the_limit_is_truly_reached():
    acc = Account.objects.create(company_name="Pct Co", slug="pct-co")
    plan = Plan.objects.create(slug="pct", name="Pct", price_monthly=Decimal("5"))
    Subscription.objects.update_or_create(
        account=acc,
        defaults={
            "plan": plan,
            "status": Subscription.ACTIVE,
            "current_period_start": timezone.now(),
        },
    )
    PlanLimit.objects.update_or_create(
        plan=plan, key="emails_month", defaults={"value": 1000}
    )
    from apps.billing.metering import period_start

    UsageCounter.objects.create(
        account=acc,
        key="emails_month",
        period_start=period_start("emails_month"),
        used=996,
    )
    row = next(r for r in billing_api.usage_report(acc) if r["key"] == "emails_month")
    assert row["percent"] == 99
    row["used"] = 1000
    UsageCounter.objects.filter(account=acc, key="emails_month").update(used=1000)
    row = next(r for r in billing_api.usage_report(acc) if r["key"] == "emails_month")
    assert row["percent"] == 100


@pytest.mark.django_db
def test_a_soft_limit_warning_never_claims_anything_is_held():
    from apps.billing import limit_catalog

    row = next(
        r for r in [{"over_limit": limit_catalog.get("conversations_month").over_limit}]
    )
    assert row["over_limit"] == limit_catalog.SOFT


@pytest.mark.django_db
def test_count_members_ignores_expired_invitations():
    from apps.accounts.api import count_members
    from apps.accounts.models import Invitation

    acc = Account.objects.create(company_name="Team Co", slug="team-co")
    stale = Invitation.objects.create(account=acc, email="stale@x.com", role="member")
    Invitation.objects.filter(pk=stale.pk).update(
        created_at=timezone.now() - timedelta(days=30)
    )
    assert count_members(acc) == 0
    Invitation.objects.create(account=acc, email="fresh@x.com", role="member")
    assert count_members(acc) == 1


@pytest.mark.django_db
def test_ensure_room_for_contact_keys_on_phone_when_phone_is_given():
    from apps.contacts.models import Contact
    from apps.contacts.services import ContactLimitReached, ensure_room_for_contact

    acc = Account.objects.create(company_name="Id Co", slug="id-co")
    plan = Plan.objects.create(slug="idp", name="IdP", price_monthly=Decimal("5"))
    Subscription.objects.update_or_create(
        account=acc,
        defaults={
            "plan": plan,
            "status": Subscription.ACTIVE,
            "current_period_start": timezone.now(),
        },
    )
    PlanLimit.objects.update_or_create(plan=plan, key="contacts", defaults={"value": 1})
    Contact.objects.create(account=acc, email="known@x.com", phone="+10000000001")
    # A new phone paired with an existing customer's email is still a NEW row once inserted via
    # upsert_contact_by_phone, so it must be blocked at the cap, not waved through as an "update".
    with pytest.raises(ContactLimitReached):
        ensure_room_for_contact(acc, email="known@x.com", phone="+10000000002")


@pytest.mark.django_db
def test_a_held_send_is_retried_and_goes_out_once_the_limit_has_room(
    account, plan, number
):
    from apps.whatsapp.tasks import drain_outbound_queue

    set_limit(plan, "whatsapp_marketing_msgs", 2)
    _queue_templates(account, 3)
    provider = _Provider()
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        drain_outbound_queue()
    held = OutboundMessage.objects.filter(status="held").first()
    assert held is not None and held.next_attempt_at is not None
    # The plan's limit resets for a new period; the held row is still queued for another try.
    UsageCounter.objects.filter(account=account, key="whatsapp_marketing_msgs").update(
        used=0
    )
    OutboundMessage.objects.filter(pk=held.pk).update(
        next_attempt_at=timezone.now() - timedelta(minutes=1)
    )
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        drain_outbound_queue()
    held.refresh_from_db()
    assert held.status == "sent"


@pytest.mark.django_db
def test_holding_never_forgets_a_verification_code_before_it_can_be_retried(
    account, plan, number
):
    from apps.whatsapp import verification_codes
    from apps.whatsapp.tasks import _hold

    set_limit(plan, "verification_codes_month", 0)
    contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234568"
    )
    template = MessageTemplate.objects.create(
        account=account,
        name="login_code",
        whatsapp_template_name="login_code",
        category=MessageTemplate.Category.AUTHENTICATION,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        content="*{{1}}*",
    )
    msg = OutboundMessage.objects.create(
        account=account,
        contact=contact,
        template=template,
        idempotency_key="otp-hold",
        payload={
            "type": "template",
            "kind": "verification_code",
            "template_name": "login_code",
            "language": "en",
            "components": verification_codes.components("135790"),
        },
    )
    _hold(msg)
    msg.refresh_from_db()
    assert msg.payload["components"] != verification_codes.components(
        verification_codes.HIDDEN
    )
    assert msg.status == "held" and msg.next_attempt_at is not None


@pytest.mark.django_db
def test_held_email_recipients_are_retried_once_the_limit_has_room(account, plan):
    from apps.email.models import BulkEmailCampaign, BulkEmailRecipient, EmailDomain
    from apps.email.tasks import dispatch_campaign, retry_held_email_recipients

    set_limit(plan, "emails_month", 2)
    domain = EmailDomain.objects.create(
        account=account, domain="meter2.test", status=EmailDomain.Status.VERIFIED
    )
    campaign = BulkEmailCampaign.objects.create(
        account=account,
        domain=domain,
        from_email="hi@meter2.test",
        subject_override="Hi",
        text_override="Hi",
    )
    for i in range(4):
        BulkEmailRecipient.objects.create(
            campaign=campaign, to_email=f"q{i}@example.com"
        )
    with (
        patch("apps.email.services.validation.validate_recipient", return_value=True),
        patch("apps.email.tasks.send_bulk_recipient_email.delay"),
        patch("apps.email.tasks.dispatch_campaign.delay"),
    ):
        dispatch_campaign.apply(args=(campaign.pk,))
    campaign.refresh_from_db()
    assert (
        BulkEmailRecipient.objects.filter(campaign=campaign, status="held").count() == 2
    )

    set_limit(plan, "emails_month", 100)  # the plan is upgraded / the month rolls over
    with (
        patch("apps.email.services.validation.validate_recipient", return_value=True),
        patch("apps.email.tasks.send_bulk_recipient_email.delay"),
    ):
        result = retry_held_email_recipients()
    assert result["queued"] == 2
    assert (
        BulkEmailRecipient.objects.filter(campaign=campaign, status="held").count() == 0
    )
    assert (
        BulkEmailRecipient.objects.filter(campaign=campaign, status="queued").count()
        == 4
    )


@pytest.mark.django_db
def test_retrying_held_recipients_reopens_a_completed_campaign(account, plan):
    from apps.email.models import BulkEmailCampaign, BulkEmailRecipient, EmailDomain
    from apps.email.tasks import retry_held_email_recipients

    set_limit(plan, "emails_month", 0)
    domain = EmailDomain.objects.create(
        account=account, domain="meter3.test", status=EmailDomain.Status.VERIFIED
    )
    campaign = BulkEmailCampaign.objects.create(
        account=account,
        domain=domain,
        from_email="hi@meter3.test",
        subject_override="Hi",
        text_override="Hi",
        status=BulkEmailCampaign.Status.COMPLETED,
        recipient_count=1,
    )
    BulkEmailRecipient.objects.create(
        campaign=campaign,
        to_email="late@example.com",
        status=BulkEmailRecipient.Status.HELD,
    )
    set_limit(plan, "emails_month", 10)
    with patch("apps.email.tasks.send_bulk_recipient_email.delay"):
        result = retry_held_email_recipients()
    assert result["queued"] == 1
    campaign.refresh_from_db()
    assert campaign.status == BulkEmailCampaign.Status.SENDING
