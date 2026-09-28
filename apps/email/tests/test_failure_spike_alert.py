"""Platform-wide send-failure-spike alerting."""

import pytest
from django.core.cache import cache

from apps.accounts.models import Account
from apps.email.models import EmailMessage
from apps.email.tasks import _FAILURE_SPIKE_MIN_VOLUME, alert_on_failure_spike


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.fixture(autouse=True)
def _clear_cooldown():
    cache.delete("email_failure_spike_alerted")
    yield
    cache.delete("email_failure_spike_alerted")


def _make(account, status, n):
    EmailMessage.objects.bulk_create(
        [
            EmailMessage(
                account=account,
                from_email="s@a.com",
                to_email=f"r{i}@x.com",
                subject="Hi",
                status=status,
            )
            for i in range(n)
        ]
    )


@pytest.mark.django_db
def test_no_alert_below_min_volume(account, monkeypatch):
    calls = []
    monkeypatch.setattr("apps.billing.slack.post_message", lambda t: calls.append(t))
    _make(account, EmailMessage.Status.FAILED, 5)
    _make(account, EmailMessage.Status.SENT, 5)
    out = alert_on_failure_spike()
    assert out["alerted"] is False
    assert calls == []


@pytest.mark.django_db
def test_no_alert_below_threshold(account, monkeypatch):
    calls = []
    monkeypatch.setattr("apps.billing.slack.post_message", lambda t: calls.append(t))
    _make(account, EmailMessage.Status.FAILED, 5)
    _make(account, EmailMessage.Status.SENT, 95)
    out = alert_on_failure_spike()
    assert out["rate"] == 0.05
    assert out["alerted"] is False


@pytest.mark.django_db
def test_alert_fires_once_then_cools_down(account, monkeypatch):
    calls = []
    monkeypatch.setattr("apps.billing.slack.post_message", lambda t: calls.append(t))
    _make(account, EmailMessage.Status.FAILED, 40)
    _make(account, EmailMessage.Status.SENT, 60)  # 40% failure over 100

    first = alert_on_failure_spike()
    assert first["alerted"] is True
    assert len(calls) == 1

    second = alert_on_failure_spike()
    assert second["alerted"] is False  # cooldown
    assert len(calls) == 1


@pytest.mark.django_db
def test_old_failures_outside_window_are_ignored(account, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    monkeypatch.setattr("apps.billing.slack.post_message", lambda t: None)
    _make(account, EmailMessage.Status.FAILED, _FAILURE_SPIKE_MIN_VOLUME + 10)
    EmailMessage.objects.update(
        created_at=timezone.now()
        - timedelta(hours=3)  # push all outside the 60-min window
    )
    out = alert_on_failure_spike()
    assert out["total"] == 0
    assert out["alerted"] is False
