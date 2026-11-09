"""Phase 7 of the UI quality programme (docs/design/ui-quality-log.md, Entry 14): Insights says
what it knows and nothing more. A missing reading is never drawn as zero, one week is never drawn
as a trend, a failed calculation says it failed, and every number says how fresh it is."""

from datetime import timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import RequestFactory
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.conversations import reporting
from apps.conversations.models import InsightGoal, WeeklySnapshot
from apps.core import charts
from apps.insights.models import BusinessPolicy, Insight

TEMPLATES = Path(settings.BASE_DIR) / "templates"


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen", slug="mwamba")


@pytest.fixture
def owner_client(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    session = client.session
    session["account_slug"] = account.slug
    session.save()
    return client


def _week(account, start, **metrics):
    base = {
        "conversations": 3,
        "answered": 2,
        "unanswered": 1,
        "median_first_reply_seconds": 300,
        "paid_orders": 1,
    }
    base.update(metrics)
    return WeeklySnapshot.objects.create(
        account=account, week_start=start, metrics=base
    )


# ---- the chart component: a missing reading is a gap ------------------------------------


class TestChartGaps:
    def test_a_missing_reading_breaks_the_line(self):
        chart = charts.line_chart(
            [("Reply", [5.0, None, 7.0, 8.0])], ["a", "b", "c", "d"]
        )
        path = chart.series[0].path
        assert path.count("M") == 2  # two runs: [5] and [7, 8]
        assert chart.series[0].points[1].y is None

    def test_a_reading_cut_off_by_gaps_is_a_visible_dot(self):
        chart = charts.line_chart(
            [("Reply", [7.0, None, 5.0, 6.0, None])], ["a", "b", "c", "d", "e"]
        )
        assert [d.value for d in chart.series[0].dots] == [7.0]
        assert (
            charts.line_chart([("Reply", [7.0, 5.0])], ["a", "b"]).series[0].dots == []
        )

    def test_a_gap_is_not_filled_down_to_the_floor(self):
        chart = charts.line_chart([("Reply", [5.0, None, 7.0])], ["a", "b", "c"])
        assert chart.series[0].area == ""

    def test_the_scale_ignores_missing_readings(self):
        chart = charts.line_chart([("Reply", [None, 10.0, 20.0])], ["a", "b", "c"])
        assert chart.low == 0 and chart.high >= 20

    def test_nothing_but_gaps_is_no_data(self):
        chart = charts.line_chart([("Reply", [None, None])], ["a", "b"])
        assert not chart.has_data

    def test_a_zero_is_still_data(self):
        chart = charts.line_chart([("Orders", [0, 0])], ["a", "b"])
        assert chart.has_data

    def _render(self, series, labels):
        return Template(
            "{% load charts %}{% line_chart series labels unit='Week' %}"
        ).render(Context({"series": series, "labels": labels}))

    def test_the_table_shows_a_dash_not_a_zero(self):
        html = self._render([("Reply", [5, None])], ["1 Jan", "8 Jan"])
        assert 'aria-label="No data">—' in html
        assert "?.value ?? 'No data'" in html  # the tooltip says so too

    def test_a_chart_with_nothing_recorded_says_so(self):
        html = self._render([("Reply", [None, None])], ["1 Jan", "8 Jan"])
        assert 'data-chart="empty"' in html
        assert "<svg" not in html


# ---- momentum ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestMomentum:
    def test_a_week_with_no_reply_to_time_is_a_gap_not_zero(self, account):
        start = timezone.now() - timedelta(days=21)
        _week(account, start)
        _week(account, start + timedelta(days=7), median_first_reply_seconds=None)
        _week(account, start + timedelta(days=14))
        reply = reporting.momentum(account)["reply_series"][0][1]
        assert reply == [5.0, None, 5.0]

    def test_a_missing_week_gets_an_empty_column(self, account):
        start = timezone.now() - timedelta(days=28)
        _week(account, start)
        _week(account, start + timedelta(days=21))
        m = reporting.momentum(account)
        assert len(m["labels"]) == 4
        assert m["paid_series"][0][1] == [1, None, None, 1]
        assert len(m["rows"]) == 2  # the table lists only what was counted

    def test_a_clock_change_is_not_a_missing_week(self, account):
        start = timezone.now() - timedelta(days=21)
        _week(account, start)
        _week(account, start + timedelta(days=7, hours=1))
        assert len(reporting.momentum(account)["labels"]) == 2

    def test_one_week_is_not_a_trend(self, account):
        _week(account, timezone.now() - timedelta(days=10))
        assert reporting.momentum(account)["enough_for_trend"] is False

    def test_insights_no_longer_repeats_the_weekly_trend(self, account, owner_client):
        """The tabs' charts are the trend (weeks over 90 days); the weekly snapshots stay
        for the weekly email."""
        start = timezone.now() - timedelta(days=17)
        _week(account, start)
        _week(account, start + timedelta(days=7))
        html = owner_client.get("/insights/").content.decode()
        assert 'id="momentum-h"' not in html and (
            "data-insights-chart" in html or 'data-insights="chart-waiting"' in html
        )


# ---- email analytics ----------------------------------------------------------------------


@pytest.mark.django_db
class TestEmailAnalytics:
    @pytest.fixture
    def ctx(self, account, monkeypatch):
        from apps.billing.limits import LimitChecker
        from apps.email.models import EmailDomain

        monkeypatch.setattr(LimitChecker, "has_feature", lambda self, key: True)
        EmailDomain.objects.create(
            account=account, domain="mail.mwamba.co", status=EmailDomain.Status.VERIFIED
        )
        request = RequestFactory().get("/insights/")

        def build():
            from apps.email.views import email_analytics_context

            return email_analytics_context(account, request)

        return build

    def test_a_day_with_no_opens_is_a_zero_column(self, ctx, monkeypatch):
        from apps.email import views

        monkeypatch.setattr(
            views,
            "_build_engagement_stats",
            lambda d: [
                {"day": "2026-10-01", "opens": 4, "clicks": 1},
                {"day": "2026-10-04", "opens": 2, "clicks": 0},
            ],
        )
        c = ctx()
        assert c["engagement_days"] == [
            "2026-10-01",
            "2026-10-02",
            "2026-10-03",
            "2026-10-04",
        ]
        assert c["engagement_series"][0] == ("Opens", [4, 0, 0, 2])

    def test_a_failed_deliverability_score_says_it_failed(self, ctx, monkeypatch):
        from apps.email.services import deliverability

        def boom(*a, **k):
            raise RuntimeError("dns down")

        monkeypatch.setattr(deliverability, "compute_score", boom)
        c = ctx()
        assert c["deliverability"] is None
        assert "Couldn't work out the deliverability score" in c["error"]


# ---- the page --------------------------------------------------------------------------


@pytest.mark.django_db
class TestInsightsPage:
    def test_the_page_says_when_its_figures_are_from(self, owner_client):
        html = owner_client.get("/insights/").content.decode()
        assert 'data-insights="as-of"' in html

    def test_each_insight_says_when_it_was_spotted(self, account, owner_client):
        Insight.objects.create(
            account=account,
            type="test_type",
            severity=Insight.Severity.WARNING,
            title="Three customers waiting",
        )
        html = owner_client.get("/insights/needs-you/").content.decode()
        item = html[html.index('data-attention="insight:test_type"') :]
        assert "Three customers waiting" in item and "started" in item

    def test_the_empty_week_does_not_assume_whatsapp(self, owner_client):
        html = owner_client.get("/insights/").content.decode()
        assert "Once WhatsApp messages start coming in" not in html
        assert "Once customers message you on a connected channel" in html

    def test_removing_a_goal_asks_first(self, account, owner_client):
        InsightGoal.objects.create(account=account, metric="leads", target=10)
        html = owner_client.get("/insights/goals/").content.decode()
        assert 'data-confirm="Remove the Leads this month goal?' in html
        assert "btn-danger-ghost" in html


# ---- policies ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPolicies:
    @pytest.fixture
    def policy(self, account):
        return BusinessPolicy.objects.create(
            account=account,
            name="Chase quiet customers",
            trigger=BusinessPolicy.Trigger.CUSTOMER_INACTIVE,
            condition={"days": 90},
            action={"type": "create_followup", "due_hours": 24},
        )

    def test_deleting_a_policy_uses_the_shared_confirm(self, policy, owner_client):
        html = owner_client.get("/insights/policies/").content.decode()
        assert 'data-confirm="Delete the policy “Chase quiet customers”?' in html
        assert "Sure?" not in html
        assert 'aria-current="page"' in html

    def test_the_editor_uses_shared_fields_with_names(self, policy, owner_client):
        html = owner_client.get(
            f"/insights/policies/{policy.pk}/edit/"
        ).content.decode()
        assert 'class="input w-24 text-center"' in html
        assert (
            '<label class="flex flex-wrap items-center gap-2 text-sm text-ink">' in html
        )
        assert 'x-data="dirtyGuard"' in html
        assert 'aria-label="Back to policies"' in html

    def test_a_saved_change_is_announced_once(self, policy, owner_client):
        resp = owner_client.post(
            f"/insights/policies/{policy.pk}/edit/",
            {"days": "60", "due_hours": "24", "note": ""},
            follow=True,
        )
        html = resp.content.decode()
        # base.html turns flash messages into toasts; the page no longer lists them as well.
        assert 'class="p-3 rounded-lg text-sm' not in html


def test_insights_templates_use_tokens_only():
    import re

    stock = re.compile(
        r"\b(?:text|bg|border)-(?:gray|red|blue|emerald|amber|brand)-\d{2,3}\b"
    )
    for path in (TEMPLATES / "insights").glob("*.html"):
        assert not stock.findall(path.read_text(encoding="utf-8")), path.name


def test_a_failed_dashboard_panel_load_shows_an_error_not_a_skeleton():
    html = (TEMPLATES / "accounts" / "dashboard.html").read_text(encoding="utf-8")
    panel = html[html.index('id="dashboard-panels"') :]
    panel = panel[: panel.index(">")]
    assert "data-error-region" in panel
    js = (Path(settings.BASE_DIR) / "static" / "js" / "app.js").read_text(
        encoding="utf-8"
    )
    assert 'region.removeAttribute("aria-busy")' in js
