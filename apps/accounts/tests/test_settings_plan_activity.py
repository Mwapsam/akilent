"""Settings leftovers (2026-11-09 review): Plan & usage and the Activity log inside the Settings
layout as panels, one billing status shared with Home, the welcome email's empty subject, the
Team tidy-ups, and Integrations / API keys in the panel header-and-footer pattern."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing import api as billing_api
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailMessage, EmailTemplate
from apps.email.services.render import render_template, validate_variables
from apps.email.starter_templates import STARTER_TEMPLATES

NOW = timezone.now()


@pytest.fixture
def shop(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _subscribe(account, **kw):
    plan = Plan.objects.create(
        slug="business", name="Business", price_monthly=Decimal("49.00")
    )
    Subscription.objects.filter(account=account).delete()
    return Subscription.objects.create(
        account=account,
        plan=plan,
        status=kw.pop("status", Subscription.ACTIVE),
        current_period_start=NOW - timedelta(days=30),
        current_period_end=kw.pop("end", NOW + timedelta(days=20)),
        **kw,
    )


def _main(page):
    return page[page.index('class="settings-main settings-main--panels"') :]


# --- Plan & usage -----------------------------------------------------------------------------


def test_plan_and_usage_is_a_settings_page_of_panels(shop):
    client, account = shop
    _subscribe(account)
    page = client.get("/billing/plans/").content.decode()
    assert 'aria-current="page">Plan &amp; usage' in page
    main = _main(page)
    for heading in ("plan-h", "usage-h", "change-plan-h", "h-compare", "cancel-h"):
        assert f'aria-labelledby="{heading}"' in main
    assert 'class="panel panel--danger" aria-labelledby="cancel-h"' in main
    assert "max-w-2xl" not in main


def test_an_overdue_renewal_reads_the_same_as_home(shop):
    """Active, but the period ended two days ago: never "Active · Renews <past date>"."""
    client, account = shop
    sub = _subscribe(account, end=NOW - timedelta(days=2))
    page = client.get("/billing/plans/").content.decode()
    assert 'data-plan-status="Renewal pending"' in page
    assert "hasn&#x27;t gone through yet" in page
    assert (
        ">Active<" not in page and "Renews " not in _main(page).split("change-plan")[0]
    )
    assert "gone through yet" in billing_api.home_notice(sub, NOW)["text"]


@pytest.mark.parametrize(
    ("status", "end_days", "cancel", "label"),
    [
        (Subscription.ACTIVE, 20, False, "Active"),
        (Subscription.ACTIVE, -1, False, "Renewal pending"),
        (Subscription.ACTIVE, 5, True, "Ending"),
        (Subscription.PAST_DUE, 5, False, "Payment failed"),
        (Subscription.TRIALING, 3, False, "Trial"),
        (Subscription.TRIALING, -1, False, "Trial ended"),
        (Subscription.INCOMPLETE, 5, False, "Payment needed"),
    ],
)
def test_plan_status(shop, status, end_days, cancel, label):
    _, account = shop
    sub = _subscribe(
        account,
        status=status,
        end=NOW + timedelta(days=end_days),
        cancel_at_period_end=cancel,
    )
    assert billing_api.plan_status(sub, NOW)["label"] == label


# --- Activity log -----------------------------------------------------------------------------


def test_activity_log_is_one_panel_without_raw_ids_or_a_filter_button(shop):
    client, account = shop
    msg = EmailMessage.objects.create(
        account=account,
        to_email="ada@example.com",
        from_email="hi@mwamba.test",
        subject="Your order",
    )
    page = client.get("/logs/messages/").content.decode()
    main = _main(page)
    assert '<section class="panel" aria-labelledby="activity-h">' in main
    assert "Your order" in main and "ada@example.com" in main
    assert f">{msg.public_id}<" not in main  # the ID lives on the detail page
    assert f'href="/logs/messages/{msg.public_id}/"' in main
    assert 'class="btn btn-primary btn-sm">Filter' not in main
    assert '<time datetime="' in main


# --- Welcome email subject --------------------------------------------------------------------


def test_the_welcome_subject_names_the_business_and_never_dangles(shop):
    _, account = shop
    starter = next(t for t in STARTER_TEMPLATES if t["slug"] == "welcome-email")
    template = EmailTemplate.objects.create(
        account=account,
        slug="welcome-test",
        name="Welcome",
        subject=starter["subject"],
        text_body=starter["text_body"],
        html_body=starter["html_body"],
    )
    assert render_template(template, {})[0] == "Welcome to Mwamba Kitchen!"
    assert (
        render_template(template, {"first_name": "Ada"})[0]
        == "Welcome to Mwamba Kitchen, Ada!"
    )
    # A value the send passes still wins, and company_name isn't reported missing.
    assert render_template(template, {"company_name": "Other"})[0].startswith(
        "Welcome to Other"
    )
    assert "company_name" not in validate_variables(template, {})


# --- Team, Integrations, API keys -------------------------------------------------------------


def test_team_invite_is_a_panel_with_its_guide_roles_and_footer(shop):
    client, _ = shop
    main = _main(client.get("/settings/team/").content.decode())
    invite = main[main.index('aria-labelledby="invite-h"') : main.index("</section>")]
    assert 'class="panel-desc">Invites expire in 7 days.' in invite
    assert "help/team" in invite or "Team guide" in invite
    assert invite.count('type="radio" name="role"') == 2 and "<select" not in invite
    assert '<div class="panel-foot">' in invite and "Send invite" in invite
    # Nothing floats above the panels.
    assert main.index('aria-labelledby="invite-h"') < main.index("Team guide")


def test_integrations_and_api_keys_use_the_panel_header_and_footer(shop):
    client, _ = shop
    integrations = _main(client.get("/settings/integrations/").content.decode())
    assert integrations.count('<div class="panel-head">') >= 2
    assert "Connect Facebook Page" in integrations
    fb = integrations[integrations.index('aria-labelledby="int-fb-h"') :]
    assert fb.index('<div class="panel-foot">') < fb.index("Connect Facebook Page")
    keys = _main(client.get("/settings/api-keys/").content.decode())
    create = keys[keys.index('aria-labelledby="create-key-h"') :]
    assert '<div class="panel-foot">' in create and "Create key" in create
