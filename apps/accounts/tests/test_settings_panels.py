"""Settings as panels (2026-11-09 review, step 1): one 720px column of panels, Save in each
panel's footer, a switch that saves itself for a single preference, one-line descriptions,
slim alerts, searchable long dropdowns, and "Plan & usage" in the side list."""

import pytest
from django.conf import settings
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership


@pytest.fixture
def owner_client(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


def test_settings_is_one_column_of_panels_with_plan_and_usage(owner_client):
    page = owner_client.get("/settings/").content.decode()
    assert 'class="settings-main settings-main--panels"' in page
    assert 'href="/billing/plans/" class="settings-nav-link">Plan &amp; usage' in page
    css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    assert ".settings-main--panels { max-width: 45rem;" in css


def test_product_updates_is_a_switch_that_saves_itself(owner_client):
    page = owner_client.get("/settings/").content.decode()
    form = page[page.index('action="/settings/product-updates/"') - 200 :]
    form = form[: form.index("</form>")]
    assert 'hx-trigger="change"' in form and 'role="switch"' in form
    assert "Save preference" in form  # only inside <noscript>
    resp = owner_client.post(
        "/settings/product-updates/",
        {"product_updates": ["off", "on"]},
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 204 and "product%20updates" in resp["X-Toast"]


def test_business_is_short_with_a_slim_alert_and_a_searchable_country(owner_client):
    page = owner_client.get("/settings/business/").content.decode()
    assert "Shown at the bottom of every campaign email" in page
    assert "panel-alert panel-alert--warning" in page
    assert 'name="country" required autocomplete="country-name" data-searchable' in page


def test_time_zone_is_searchable_and_tools_are_switches(owner_client):
    hours = owner_client.get("/settings/hours/").content.decode()
    assert 'name="timezone" class="select" data-searchable' in hours
    src = (
        settings.BASE_DIR / "templates" / "accounts" / "settings_tools.html"
    ).read_text(encoding="utf-8")
    assert 'role="switch" class="switch' in src and "rounded-xl border" not in src


def test_ai_settings_are_separate_panels_without_boxes_inside(owner_client):
    page = owner_client.get("/settings/ai/").content.decode()
    for title in ("AI replies", "Knowledge base", "What is sent to the AI service"):
        assert f">{title}</h2>" in page
    src = (settings.BASE_DIR / "templates" / "accounts" / "settings_ai.html").read_text(
        encoding="utf-8"
    )
    assert "rounded-xl border" not in src and "_save_bar.html" not in src


def test_search_select_and_panel_save_are_wired():
    js = (settings.BASE_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "function enhanceSearchSelect(sel)" in js
    assert "select[data-searchable]" in js and "syncPanelSave" in js
