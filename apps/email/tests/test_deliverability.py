from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.email.models import (
    DeliverabilitySnapshot,
    EmailDomain,
    SendReputation,
    SuppressionListEntry,
)
from apps.email.services.deliverability import compute_score, snapshot
from apps.logs.models import MessageStatsDaily


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


def _verified_domain(account, **kw):
    defaults = dict(
        domain="mail.acme.com",
        status=EmailDomain.Status.VERIFIED,
        spf_ok=True,
        dkim_ok=True,
        dmarc_ok=True,
    )
    defaults.update(kw)
    return EmailDomain.objects.create(account=account, **defaults)


def _stats(account, domain, *, sent, delivered=0, bounced=0, complained=0, opened=0, days_ago=1):
    MessageStatsDaily.objects.create(
        account=account,
        domain=domain,
        day=(timezone.now() - timedelta(days=days_ago)).date(),
        key_mode="live",
        sent=sent,
        delivered=delivered,
        bounced=bounced,
        complained=complained,
        opened=opened,
        unique_opens=opened,
    )


@pytest.mark.django_db
def test_clean_setup_scores_high(account):
    d = _verified_domain(account)
    _stats(account, d, sent=1000, delivered=990, bounced=6, complained=0, opened=300)
    result = compute_score(account, d)
    assert result.score >= 90
    assert result.grade == "Excellent"
    # No urgent recommendations — only the optional "tighten DMARC" nudge (the
    # model's generated DMARC record is p=none) is acceptable.
    urgent = [r for r in result.recommendations if "Tighten DMARC" not in r]
    assert urgent == []


@pytest.mark.django_db
def test_missing_dkim_fails_that_check_and_recommends_fix(account):
    d = _verified_domain(account, dkim_ok=False)
    _stats(account, d, sent=500, delivered=490, bounced=5)
    result = compute_score(account, d)
    dkim = next(c for c in result.checks if c.key == "dkim")
    assert dkim.status == "fail"
    assert any("DKIM" in r for r in result.recommendations)
    assert result.score < 90


@pytest.mark.django_db
def test_high_bounce_rate_fails_and_drags_score(account):
    d = _verified_domain(account)
    _stats(account, d, sent=1000, delivered=900, bounced=90)  # 9%
    result = compute_score(account, d)
    bounce = next(c for c in result.checks if c.key == "bounce_rate")
    assert bounce.status == "fail"
    assert any("list" in r.lower() for r in result.recommendations)


@pytest.mark.django_db
def test_low_volume_marks_rate_checks_na(account):
    d = _verified_domain(account)
    _stats(account, d, sent=10, delivered=10)
    result = compute_score(account, d)
    assert next(c for c in result.checks if c.key == "bounce_rate").status == "na"
    # auth still scored, so a fully-authed low-volume domain is still "good"
    assert result.score >= 75


@pytest.mark.django_db
def test_halted_reputation_fails_check(account):
    d = _verified_domain(account)
    _stats(account, d, sent=500, delivered=480, bounced=5)
    SendReputation.objects.create(
        account=account, state=SendReputation.State.HALTED, halted_reason="bounce rate 7%"
    )
    result = compute_score(account, d)
    rep = next(c for c in result.checks if c.key == "sending_reputation")
    assert rep.status == "fail"
    assert any("halted" in r.lower() for r in result.recommendations)


@pytest.mark.django_db
def test_snapshot_upserts_one_row_per_day(account):
    d = _verified_domain(account)
    _stats(account, d, sent=200, delivered=195)
    snapshot(account, d)
    snapshot(account, d)
    rows = DeliverabilitySnapshot.objects.filter(account=account, domain=d)
    assert rows.count() == 1
    assert rows.first().score > 0


@pytest.mark.django_db
def test_week_over_week_bounce_spike_recommendation(account):
    d = _verified_domain(account)
    # last week's snapshot recorded a 1.0% bounce rate
    DeliverabilitySnapshot.objects.create(
        account=account, domain=d, day=(timezone.now() - timedelta(days=7)).date(),
        score=80, grade="Good",
        checks=[{"key": "bounce_rate", "status": "warn", "detail": "1.0% — above the 2% comfort zone"}],
    )
    _stats(account, d, sent=1000, delivered=960, bounced=35)  # 3.5%
    result = compute_score(account, d)
    assert any("rose" in r and "last week" in r for r in result.recommendations)


@pytest.mark.django_db
def test_new_suppressions_hurt_list_quality(account):
    d = _verified_domain(account)
    _stats(account, d, sent=1000, delivered=950, days_ago=1)
    _stats(account, d, sent=500, delivered=480, days_ago=3)
    for i in range(60):
        SuppressionListEntry.objects.create(
            account=account, email=f"b{i}@x.com", reason=SuppressionListEntry.Reason.BOUNCE
        )
    result = compute_score(account, d)
    lq = next(c for c in result.checks if c.key == "list_quality")
    assert lq.status in ("warn", "fail")
