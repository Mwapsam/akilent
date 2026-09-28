"""Sender-reputation circuit breaker."""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.core.models import MailProviderSettings
from apps.email.models import (
    BulkEmailCampaign,
    EmailDomain,
    EmailMessage,
    SendReputation,
)
from apps.email.services import reputation


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.fixture(autouse=True)
def low_min_volume(db):
    s = MailProviderSettings.load()
    s.reputation_min_volume = 10
    s.reputation_bounce_warn = 0.05
    s.reputation_bounce_halt = 0.10
    s.reputation_complaint_halt = 0.005
    s.save()


@pytest.mark.django_db
def test_below_min_volume_never_halts(account):
    for _ in range(5):
        reputation.record_send(account)
    reputation.record_bounce(account)
    reputation.record_bounce(account)
    rep = SendReputation.objects.get(account=account)
    assert rep.bounce_rate > 0.10  # 2/5
    assert rep.state == SendReputation.State.OK  # but volume < min
    assert reputation.check_can_send(account) == (True, "")


@pytest.mark.django_db
def test_bounce_rate_halts_and_blocks_non_system(account):
    for _ in range(100):
        reputation.record_send(account)
    for _ in range(11):  # 11% > 10% halt
        reputation.record_bounce(account)

    rep = SendReputation.objects.get(account=account)
    assert rep.state == SendReputation.State.HALTED

    allowed, reason = reputation.check_can_send(account)
    assert allowed is False
    assert "bounce rate" in reason

    # System mail is exempt.
    assert reputation.check_can_send(account, system=True) == (True, "")


@pytest.mark.django_db
def test_warn_then_halt_transitions(account):
    for _ in range(100):
        reputation.record_send(account)
    for _ in range(6):  # 6% -> warn (>=5%, <10%)
        reputation.record_bounce(account)
    assert (
        SendReputation.objects.get(account=account).state == SendReputation.State.WARNED
    )

    for _ in range(5):  # now 11% -> halt
        reputation.record_bounce(account)
    assert (
        SendReputation.objects.get(account=account).state == SendReputation.State.HALTED
    )


@pytest.mark.django_db
def test_complaint_rate_halts(account):
    for _ in range(1000):
        reputation.record_send(account)
    for _ in range(6):  # 0.6% > 0.5% complaint halt
        reputation.record_complaint(account)
    assert (
        SendReputation.objects.get(account=account).state == SendReputation.State.HALTED
    )


@pytest.mark.django_db
def test_reset_clears_halt(account):
    for _ in range(100):
        reputation.record_send(account)
    for _ in range(20):
        reputation.record_bounce(account)
    assert reputation.check_can_send(account)[0] is False

    reputation.reset(account)
    rep = SendReputation.objects.get(account=account)
    assert rep.state == SendReputation.State.OK
    assert rep.sent == 0 and rep.bounced == 0
    assert reputation.check_can_send(account) == (True, "")


@pytest.mark.django_db
def test_halt_fires_slack_alert_once(account, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "apps.billing.slack.post_message", lambda text: calls.append(text)
    )

    for _ in range(100):
        reputation.record_send(account)
    for _ in range(11):
        reputation.record_bounce(account)
    for _ in range(3):  # further bounces must not re-alert
        reputation.record_bounce(account)

    assert len(calls) == 1
    assert "reputation halt" in calls[0].lower()


@pytest.mark.django_db
def test_send_task_drops_message_for_halted_account(account, monkeypatch):
    SendReputation.objects.create(
        account=account,
        state=SendReputation.State.HALTED,
        halted_reason="bounce rate 12%",
    )
    msg = EmailMessage.objects.create(
        account=account,
        from_email="a@acme.com",
        to_email="r@x.com",
        subject="Hi",
    )
    monkeypatch.setattr(
        "apps.email.services.suppression.is_suppressed", lambda acc, em: False
    )
    sent = []
    monkeypatch.setattr(
        "apps.email.tasks.get_send_provider",
        lambda: type("P", (), {"send": lambda self, o: sent.append(o)})(),
    )

    from apps.email.tasks import _send_email_message

    class _Task:
        class request:
            retries = 0

    _send_email_message(_Task, msg, "t", "")
    msg.refresh_from_db()
    assert msg.status == EmailMessage.Status.FAILED
    assert "reputation halt" in msg.error.lower()
    assert sent == []  # provider never called


@pytest.mark.django_db
def test_dispatch_campaign_pauses_for_halted_account(account, monkeypatch):
    domain = EmailDomain.objects.create(
        account=account, domain="acme.com", status=EmailDomain.Status.VERIFIED
    )
    campaign = BulkEmailCampaign.objects.create(
        account=account,
        domain=domain,
        from_email="n@acme.com",
        subject_override="Hi",
        status=BulkEmailCampaign.Status.QUEUED,
    )
    SendReputation.objects.create(
        account=account,
        state=SendReputation.State.HALTED,
        halted_reason="bounce rate 12%",
    )

    from apps.email.tasks import dispatch_campaign

    dispatch_campaign.run(campaign.pk)
    campaign.refresh_from_db()
    assert campaign.status == BulkEmailCampaign.Status.PAUSED
    assert "reputation halt" in campaign.error.lower()
