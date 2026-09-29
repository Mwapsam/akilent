"""Renders /insights/ with every "there is data" branch populated: peak hours, AI, automation
credit, channels table, business hours. A template bug in any of these only shows up with real
data behind it — the thin scoping tests in test_insights_view.py don't reach these branches."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts import business_hours
from apps.accounts.models import Account, Membership
from apps.ai.models import AIProposal, AISettings
from apps.automation.models import Workflow, WorkflowRun
from apps.billing.models import Plan, Subscription
from apps.commerce.models import Order
from apps.contacts.models import Contact
from apps.conversations import snapshots
from apps.conversations.models import Conversation, ConversationAttribution, Message
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


def _subscribe(account):
    plan = Plan.objects.create(
        slug="business-full",
        name="Business",
        price_monthly=Decimal("99"),
        detailed_analytics=True,
    )
    return Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.ACTIVE,
        current_period_start=NOW - timedelta(days=10),
    )


def _enquiry(account, at, *, reply_after=None, sent_by="", assigned_to=None):
    contact = Contact.objects.create(
        account=account, phone=f"+26097{Contact.objects.count():07d}"
    )
    conv = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp", assigned_to=assigned_to
    )
    Message.objects.create(
        account=account, conversation=conv, direction=IN, body="hi", timestamp=at
    )
    if reply_after is not None:
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="reply",
            status="sent",
            timestamp=at + reply_after,
            metadata={"sent_by": sent_by} if sent_by else {},
        )
    return conv


@pytest.mark.django_db
def test_every_populated_branch_renders(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    _subscribe(account)

    # Business hours, so the automation pillar's out-of-hours tile and the peak-hours
    # closed-share line both have something to report.
    business_hours.save_hours(
        account, tz="UTC", schedule={"mon": {"open": "09:00", "close": "17:00"}}
    )

    # 25 enquiries (over the peak-hours threshold), assigned to a team member, some
    # answered by a person and some by an automation/AI, so team() and peak_hours() both render.
    for i in range(25):
        _enquiry(
            account,
            NOW - timedelta(hours=1, minutes=i),
            reply_after=timedelta(minutes=2),
            sent_by="automation" if i % 2 else "",
            assigned_to=user,
        )

    # An automation credited with a paid order, so the "automations that led to sales" table
    # and the channels table both have a row.
    workflow = Workflow.objects.create(
        account=account, name="Greeter", slug="greeter", status="published"
    )
    run = WorkflowRun.objects.create(
        workflow=workflow,
        contact=Contact.objects.create(account=account, phone="+2609700x"),
    )
    paid_conv = _enquiry(
        account, NOW - timedelta(hours=2), reply_after=timedelta(minutes=1)
    )
    order = Order.objects.create(
        account=account,
        contact=paid_conv.contact,
        conversation=paid_conv,
        status=Order.Status.PAID,
        currency="USD",
        total=Decimal("30.00"),
        paid_at=NOW - timedelta(hours=1),
    )
    ConversationAttribution.objects.create(
        account=account,
        conversation=paid_conv,
        channel="whatsapp",
        order=order,
        workflow_run=run,
        method=ConversationAttribution.Method.RECENT_CONVERSATION,
    )

    # AI on, with a used and a dismissed proposal, so the AI tile renders its used_pct.
    AISettings.objects.create(account=account, enabled=True)
    AIProposal.objects.create(
        account=account,
        status=AIProposal.Status.USED,
        ready_at=NOW,
        edited_before_send=False,
    )
    AIProposal.objects.create(
        account=account, status=AIProposal.Status.DISMISSED, ready_at=NOW
    )

    # A connected WhatsApp number far enough back to have closed, settled weeks, so the
    # Momentum pillar's charts and table both have rows.
    number = WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id="PN-full",
        waba_id="W",
        access_token="t",
        is_active=True,
    )
    connected = NOW - timedelta(days=20)
    WhatsAppBusinessNumber.objects.filter(pk=number.pk).update(created_at=connected)
    week1_start = snapshots.closed_week_starts(connected, NOW)[0]
    _enquiry(
        account, week1_start + timedelta(hours=1), reply_after=timedelta(minutes=3)
    )
    snapshots.capture(account, connected, NOW)

    resp = client.get("/insights/?period=90")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Your busiest time is" in body
    assert "out-of-hours customers reached" in body
    assert "Greeter" in body  # per-workflow sales table
    assert "AI suggestions used" in body
    assert "Team member" in body
    # Channel performance table renders (plan has detailed_analytics), not the upgrade teaser.
    assert "Channel performance is available on higher plans" not in body
    assert "Median first reply" in body  # momentum chart title
    assert (
        "Weekly numbers since your first captured week" in body
    )  # momentum table caption
