"""Workstream F analytics tests.

Tests cover:
 1. Simple lead conversion (3 leads, 2 reach qualified)
 2. Time in stage (A→B→C, verify durations)
 3. Win/loss rate (2 won, 1 lost)
 4. Reopened deal (win → reopen → lose): final state is "lost"
 5. Pipeline value (2 deals, different currencies)
 6. Owner performance (attributed to owner_at_transition)
 7. Empty range: all metrics return 0/None
 8. DST boundary: transition crossing DST is UTC-correct
 9. set_lead_status writes a StageTransition in the same transaction
10. create_lead writes a creation StageTransition
11. Backfill is idempotent
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.crm.models import Deal, Lead, Pipeline, StageTransition
from apps.crm.services import create_lead, set_lead_status

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Test Co")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971000001")


@pytest.fixture
def pipeline(account):
    return Pipeline.ensure_default(account)


def _make_lead(account, contact, extra_phone_digit="2"):
    c = Contact.objects.create(
        account=account, phone=f"+26097100{extra_phone_digit}001"
    )
    return Lead.objects.create(account=account, contact=c, status=Lead.Status.NEW)


def _st(
    account,
    *,
    lead=None,
    deal=None,
    from_value,
    to_value,
    occurred_at,
    owner=None,
    provenance="observed",
):
    return StageTransition.objects.create(
        account=account,
        lead=lead,
        deal=deal,
        from_value=from_value,
        to_value=to_value,
        occurred_at=occurred_at,
        owner_at_transition=owner,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Test 1: Simple lead conversion
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_lead_conversion_simple(account, contact):
    from apps.crm.analytics import lead_conversion

    t_start = datetime(2026, 1, 1, tzinfo=UTC)
    t_end = datetime(2026, 2, 1, tzinfo=UTC)

    leads = []
    for i in range(3):
        c = Contact.objects.create(account=account, phone=f"+2609700000{i}")
        lead = Lead.objects.create(account=account, contact=c, status="new")
        leads.append(lead)
        _st(
            account,
            lead=lead,
            from_value="",
            to_value="new",
            occurred_at=datetime(2026, 1, 10, tzinfo=UTC),
        )

    # 2 of 3 reach qualified
    for lead in leads[:2]:
        _st(
            account,
            lead=lead,
            from_value="new",
            to_value="qualified",
            occurred_at=datetime(2026, 1, 15, tzinfo=UTC),
        )

    result = lead_conversion(account, t_start, t_end)
    assert result["denominator"] == 3
    assert result["numerator"] == 2
    assert abs(result["rate"] - 2 / 3) < 1e-9


# ---------------------------------------------------------------------------
# Test 2: Time in stage (A → B → C)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_time_in_stage(account, contact):
    from apps.crm.analytics import time_in_stage

    lead = Lead.objects.create(account=account, contact=contact, status="new")
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)  # 2h in "new"
    t2 = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)  # 3h in "contacted"

    _st(account, lead=lead, from_value="", to_value="new", occurred_at=t0)
    _st(account, lead=lead, from_value="new", to_value="contacted", occurred_at=t1)
    _st(
        account, lead=lead, from_value="contacted", to_value="qualified", occurred_at=t2
    )

    result_new = time_in_stage(account, "new", lead_or_deal="lead")
    assert len(result_new["closed"]) == 1
    assert result_new["closed"][0]["duration_seconds"] == 2 * 3600
    assert result_new["open"] == []

    result_contacted = time_in_stage(account, "contacted", lead_or_deal="lead")
    assert len(result_contacted["closed"]) == 1
    assert result_contacted["closed"][0]["duration_seconds"] == 3 * 3600

    result_qualified = time_in_stage(account, "qualified", lead_or_deal="lead")
    assert result_qualified["closed"] == []
    assert len(result_qualified["open"]) == 1


# ---------------------------------------------------------------------------
# Test 3: Win/loss rate (2 won, 1 lost in period)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_win_rate_basic(account, contact, pipeline):
    from apps.crm.analytics import win_rate

    t_start = datetime(2026, 3, 1, tzinfo=UTC)
    t_end = datetime(2026, 4, 1, tzinfo=UTC)

    won_stage = pipeline.stages.get(is_won=True)
    lost_stage = pipeline.stages.get(is_lost=True)

    for i in range(2):
        c = Contact.objects.create(account=account, phone=f"+2609700010{i}")
        deal = Deal.objects.create(
            account=account,
            contact=c,
            pipeline=pipeline,
            stage=pipeline.stages.first(),
            title=f"Won deal {i}",
            value=100,
        )
        _st(
            account,
            deal=deal,
            from_value="",
            to_value="New",
            occurred_at=datetime(2026, 3, 5, tzinfo=UTC),
        )
        _st(
            account,
            deal=deal,
            from_value="New",
            to_value=won_stage.name,
            occurred_at=datetime(2026, 3, 20, tzinfo=UTC),
        )

    c3 = Contact.objects.create(account=account, phone="+260970002003")
    deal_l = Deal.objects.create(
        account=account,
        contact=c3,
        pipeline=pipeline,
        stage=pipeline.stages.first(),
        title="Lost deal",
        value=50,
    )
    _st(
        account,
        deal=deal_l,
        from_value="",
        to_value="New",
        occurred_at=datetime(2026, 3, 5, tzinfo=UTC),
    )
    _st(
        account,
        deal=deal_l,
        from_value="New",
        to_value=lost_stage.name,
        occurred_at=datetime(2026, 3, 25, tzinfo=UTC),
    )

    result = win_rate(account, t_start, t_end)
    assert result["won"] == 2
    assert result["lost"] == 1
    assert abs(result["rate"] - 2 / 3) < 1e-9


# ---------------------------------------------------------------------------
# Test 4: Reopened deal (win → reopen → lose)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_win_rate_reopened_deal_final_state_lost(account, contact, pipeline):
    from apps.crm.analytics import win_rate

    t_start = datetime(2026, 5, 1, tzinfo=UTC)
    t_end = datetime(2026, 6, 1, tzinfo=UTC)

    won_stage = pipeline.stages.get(is_won=True)
    lost_stage = pipeline.stages.get(is_lost=True)
    open_stage = pipeline.stages.filter(is_won=False, is_lost=False).first()

    deal = Deal.objects.create(
        account=account,
        contact=contact,
        pipeline=pipeline,
        stage=open_stage,
        title="Reopen deal",
        value=200,
    )
    _st(
        account,
        deal=deal,
        from_value="",
        to_value=open_stage.name,
        occurred_at=datetime(2026, 5, 2, tzinfo=UTC),
    )
    _st(
        account,
        deal=deal,
        from_value=open_stage.name,
        to_value=won_stage.name,
        occurred_at=datetime(2026, 5, 10, tzinfo=UTC),
    )
    _st(
        account,
        deal=deal,
        from_value=won_stage.name,
        to_value=open_stage.name,
        occurred_at=datetime(2026, 5, 15, tzinfo=UTC),
    )
    _st(
        account,
        deal=deal,
        from_value=open_stage.name,
        to_value=lost_stage.name,
        occurred_at=datetime(2026, 5, 20, tzinfo=UTC),
    )

    result = win_rate(account, t_start, t_end)
    assert result["won"] == 0
    assert result["lost"] == 1


# ---------------------------------------------------------------------------
# Test 5: Pipeline value (2 currencies)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_pipeline_value(account, contact, pipeline):
    from apps.crm.analytics import pipeline_value

    open_stage = pipeline.stages.filter(is_won=False, is_lost=False).first()

    c1 = Contact.objects.create(account=account, phone="+260970003001")
    c2 = Contact.objects.create(account=account, phone="+260970003002")
    Deal.objects.create(
        account=account,
        contact=c1,
        pipeline=pipeline,
        stage=open_stage,
        title="USD deal",
        value=Decimal("1000.00"),
        currency="USD",
        status=Deal.Status.OPEN,
    )
    Deal.objects.create(
        account=account,
        contact=c2,
        pipeline=pipeline,
        stage=open_stage,
        title="USD deal 2",
        value=Decimal("500.00"),
        currency="USD",
        status=Deal.Status.OPEN,
    )
    c3 = Contact.objects.create(account=account, phone="+260970003003")
    Deal.objects.create(
        account=account,
        contact=c3,
        pipeline=pipeline,
        stage=open_stage,
        title="ZMW deal",
        value=Decimal("25000.00"),
        currency="ZMW",
        status=Deal.Status.OPEN,
    )

    result = pipeline_value(account)
    by_currency = {r["currency"]: r["total"] for r in result}
    assert by_currency["USD"] == Decimal("1500.00")
    assert by_currency["ZMW"] == Decimal("25000.00")


# ---------------------------------------------------------------------------
# Test 6: Owner performance
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_owner_performance(account, contact):
    from django.contrib.auth import get_user_model

    from apps.crm.analytics import owner_performance

    User = get_user_model()
    alice = User.objects.create_user("alice_perf", password="x")
    bob = User.objects.create_user("bob_perf", password="x")

    t_start = datetime(2026, 7, 1, tzinfo=UTC)
    t_end = datetime(2026, 8, 1, tzinfo=UTC)

    lead = Lead.objects.create(account=account, contact=contact, status="new")
    # Alice owns 3 transitions, 1 conversion; Bob owns 1 transition, 1 conversion
    for val, owner in [
        ("new", alice),
        ("contacted", alice),
        ("qualified", alice),
        ("qualified", bob),
    ]:
        from_val = (
            "new" if val == "contacted" else ("contacted" if val == "qualified" else "")
        )
        _st(
            account,
            lead=lead,
            from_value=from_val,
            to_value=val,
            occurred_at=datetime(2026, 7, 10, tzinfo=UTC),
            owner=owner,
        )

    result = owner_performance(account, t_start, t_end)
    alice_row = next(r for r in result if r["owner_id"] == alice.id)
    bob_row = next(r for r in result if r["owner_id"] == bob.id)

    assert alice_row["transitions"] == 3
    assert alice_row["conversions"] == 1
    assert bob_row["transitions"] == 1
    assert bob_row["conversions"] == 1


# ---------------------------------------------------------------------------
# Test 7: Empty range
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_empty_range_returns_zero(account, contact, pipeline):
    from apps.crm.analytics import (
        lead_conversion,
        owner_performance,
        pipeline_value,
        win_rate,
    )

    t_start = datetime(2025, 1, 1, tzinfo=UTC)
    t_end = datetime(2025, 2, 1, tzinfo=UTC)

    lc = lead_conversion(account, t_start, t_end)
    assert lc["denominator"] == 0
    assert lc["rate"] is None

    wr = win_rate(account, t_start, t_end)
    assert wr["won"] == 0
    assert wr["lost"] == 0
    assert wr["rate"] is None

    pv = pipeline_value(account)
    assert pv == []

    op = owner_performance(account, t_start, t_end)
    assert op == []


# ---------------------------------------------------------------------------
# Test 8: DST boundary (UTC correctness)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_dst_boundary_utc_correct(account, contact):
    """Transition that falls in DST gap (clocks spring forward) is still
    found by the UTC range query without off-by-one errors."""
    from apps.crm.analytics import lead_conversion

    # US Eastern DST 2026: clocks spring forward 2026-03-08 at 02:00 → 03:00
    # We create a lead at 01:30 ET = 06:30 UTC, query range = [06:00, 07:00) UTC
    lead = Lead.objects.create(account=account, contact=contact, status="new")
    # 2026-03-08 01:30 ET = 06:30 UTC (before spring forward)
    et = ZoneInfo("America/New_York")
    occurred = datetime(2026, 3, 8, 1, 30, tzinfo=et)  # aware ET

    _st(account, lead=lead, from_value="", to_value="new", occurred_at=occurred)

    t_start = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    t_end = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)

    result = lead_conversion(account, t_start, t_end)
    assert result["denominator"] == 1  # found in [06:00, 07:00) UTC


# ---------------------------------------------------------------------------
# Test 9: set_lead_status writes StageTransition
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_set_lead_status_writes_stage_transition(account, contact):
    lead = create_lead(account, contact)
    initial_count = StageTransition.objects.filter(lead=lead).count()

    set_lead_status(lead, "contacted")

    transitions = StageTransition.objects.filter(lead=lead).order_by("occurred_at")
    assert transitions.count() == initial_count + 1
    latest = transitions.last()
    assert latest.from_value == "new"
    assert latest.to_value == "contacted"
    assert latest.provenance == StageTransition.Provenance.OBSERVED


@pytest.mark.django_db
def test_a_stale_copy_of_the_lead_records_no_second_transition(account, contact):
    from apps.crm.models import Lead

    lead = create_lead(account, contact)
    stale = Lead.objects.get(pk=lead.pk)  # another person's copy, read as "new"
    set_lead_status(lead, "contacted")
    before = StageTransition.objects.filter(lead=lead).count()

    assert set_lead_status(stale, "qualified") is False
    assert StageTransition.objects.filter(lead=lead).count() == before
    stale.refresh_from_db()
    assert stale.status == "contacted"


@pytest.mark.django_db
def test_status_is_not_changed_if_its_event_cannot_be_recorded(account, contact):
    from unittest.mock import patch

    from apps.crm.models import Lead

    lead = create_lead(account, contact)
    with (
        patch("apps.crm.services.emit_event", side_effect=RuntimeError("db down")),
        pytest.raises(RuntimeError),
    ):
        set_lead_status(lead, "contacted")
    assert Lead.objects.get(pk=lead.pk).status == "new"


# ---------------------------------------------------------------------------
# Test 10: create_lead writes a creation StageTransition
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_lead_writes_creation_transition(account):
    c = Contact.objects.create(account=account, phone="+260970009901")
    lead = create_lead(account, c, source="test")

    creation = StageTransition.objects.filter(lead=lead, from_value="").first()
    assert creation is not None
    assert creation.to_value == Lead.Status.NEW
    assert creation.provenance == StageTransition.Provenance.OBSERVED
    assert creation.source_event_id.startswith("evt:")


# ---------------------------------------------------------------------------
# Test 11: Backfill is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_backfill_idempotent(account, contact):
    from django.core.management import call_command

    # Create a lead with a status_changed event (via the service which also creates a StageTransition)
    lead = create_lead(account, contact)
    set_lead_status(lead, "contacted")

    count_before = StageTransition.objects.count()

    call_command("backfill_stage_transitions", verbosity=0)
    count_after_first = StageTransition.objects.count()

    call_command("backfill_stage_transitions", verbosity=0)
    count_after_second = StageTransition.objects.count()

    # Running twice should produce the same count (idempotent)
    assert count_after_second == count_after_first
    # Count should be >= count_before (backfill may add estimated rows)
    assert count_after_first >= count_before
