"""The sidebar's six sections, their tabs, the Inbox badge, Inbox settings and Ctrl+K
(apps/core/sections.py)."""

import json
import re

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership, Team
from apps.core import sections

ALL_ON = {
    "enabled_channels": {"whatsapp", "instagram", "facebook", "email"},
    "CAMPAIGNS_ENABLED": True,
    "released": {"support": True},
    "locked_features": set(),
    "tools_off": set(),
}


def _labels(nav):
    return [s["label"] for s in nav["sections"]]


def _tabs(nav, key):
    return [
        t["label"] for t in next(s for s in nav["sections"] if s["key"] == key)["tabs"]
    ]


# ── The registry ────────────────────────────────────────────────────────────


def test_six_main_items_when_everything_is_on():
    nav = sections.build(ALL_ON, "/dashboard/")
    assert _labels(nav) == [
        "Home",
        "Inbox",
        "Customers",
        "Campaigns",
        "Content Studio",
        "Automations",
    ]
    assert _tabs(nav, "inbox") == ["Conversations", "Comments", "Follow-ups"]
    assert _tabs(nav, "customers") == ["All customers", "Interested", "Orders"]
    assert _tabs(nav, "automations") == [
        "Automations",
        "Forms",
        "Build with AI",
        "Why didn't it reply?",
    ]


@pytest.mark.parametrize(
    "path, section, tab",
    [
        ("/dashboard/", "home", "overview"),
        ("/insights/", "home", "insights"),
        ("/inbox/", "inbox", "conversations"),
        ("/inbox/c/abc/", "inbox", "conversations"),
        ("/inbox/comments/", "inbox", "comments"),
        ("/instagram/rules/", "inbox", "comments"),
        ("/inbox/followups/", "inbox", "followups"),
        ("/contacts/", "customers", "all"),
        ("/contacts/abc/", "customers", "all"),
        ("/sales/", "customers", "interested"),
        ("/orders/", "customers", "orders"),
        ("/campaigns/", "campaigns", "campaigns"),
        ("/email/campaigns/new/", "campaigns", "campaigns"),
        ("/email/templates/", "campaigns", "templates"),
        ("/whatsapp/templates/create/", "campaigns", "templates"),
        ("/publishing/calendar/", "studio", "studio"),
        ("/automations/", "automations", "automations"),
        ("/automations/why-not/", "automations", "why_not"),
        ("/inbox/forms/", "automations", "forms"),
        ("/inbox/forms/3/", "automations", "forms"),
        ("/build/", "automations", "build"),
    ],
)
def test_every_old_url_lights_its_section_and_tab(path, section, tab):
    nav = sections.build(ALL_ON, path)
    assert nav["current"]["key"] == section
    assert [t["key"] for t in nav["current"]["tabs"] if t["active"]] == [tab]
    assert sum(s["active"] for s in nav["sections"]) == 1


@pytest.mark.parametrize(
    "path", ["/inbox/saved-replies/", "/inbox/settings/teams/", "/inbox/teams/3/"]
)
def test_inbox_settings_light_inbox_without_a_second_tab_bar(path):
    nav = sections.build(ALL_ON, path)
    assert nav["current"]["key"] == "inbox"
    assert not nav["current"]["show_tab_bar"]
    assert not any(t["active"] for t in nav["current"]["tabs"])


def test_pages_outside_the_sections_light_none():
    for path in ("/channels/", "/settings/", "/logs/messages/", "/help/", "/manage/"):
        assert sections.build(ALL_ON, path)["current"] is None


def test_comments_need_instagram_or_facebook():
    nav = sections.build({**ALL_ON, "enabled_channels": {"whatsapp"}}, "/inbox/")
    assert _tabs(nav, "inbox") == ["Conversations", "Follow-ups"]


def test_campaigns_need_a_sending_channel():
    nav = sections.build({**ALL_ON, "CAMPAIGNS_ENABLED": False}, "/dashboard/")
    assert "Campaigns" not in _labels(nav)


def test_switched_off_tools_are_hidden():
    nav = sections.build({**ALL_ON, "tools_off": {"sales", "orders"}}, "/contacts/")
    assert _tabs(nav, "customers") == ["All customers"]
    assert not nav["current"]["show_tab_bar"]  # one tab: no bar


def test_a_locked_tab_opens_the_locked_page():
    nav = sections.build({**ALL_ON, "locked_features": {"sales"}}, "/contacts/")
    interested = next(t for t in nav["current"]["tabs"] if t["key"] == "interested")
    assert interested["locked"] and interested["href"] == "/billing/locked/sales/"


def test_a_section_with_every_tab_locked_is_hidden():
    nav = sections.build(
        {**ALL_ON, "locked_features": {"automations", "content_studio"}}, "/dashboard/"
    )
    assert "Automations" not in _labels(nav)
    assert "Content Studio" not in _labels(nav)


def test_a_main_item_opens_its_first_usable_tab():
    nav = sections.build({**ALL_ON, "locked_features": {"sales"}}, "/dashboard/")
    assert (
        next(s for s in nav["sections"] if s["key"] == "customers")["href"]
        == "/contacts/"
    )


def test_the_browser_rule_marks_one_main_item():
    """Inbox's /inbox/ prefix mustn't claim Automations' /inbox/forms/ on Back."""
    inbox = next(s for s in sections.SECTIONS if s.key == "inbox")
    assert "inbox/forms" in sections.section_excludes(inbox)


# ── Pages ───────────────────────────────────────────────────────────────────


@pytest.fixture
def owner(db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    account = Account.objects.create(
        company_name="Acme",
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=True,
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user, account


@pytest.fixture
def owner_client(client, owner):
    client.force_login(owner[0])
    return client


def _nav_html(html: str) -> str:
    return html[html.index('aria-label="Primary"') :][:20000]


def test_the_sidebar_keeps_connections_and_settings(owner_client):
    html = _nav_html(owner_client.get("/dashboard/").content.decode())
    assert ">Home<" in html and ">Inbox<" in html and ">Customers<" in html
    assert ">Settings<" in html and ">Help<" in html
    assert "Message history" not in html
    assert ">Billing<" not in html


def test_billing_is_in_the_account_menu(owner_client):
    html = owner_client.get("/dashboard/").content.decode()
    assert "Billing &amp; plans" in html[html.index('id="user-menu-panel"') :]


def test_the_section_tab_bar_shows_on_a_section_page(owner_client):
    html = owner_client.get("/dashboard/").content.decode()
    assert 'class="section-tabs ' in html
    assert re.search(r'href="/insights/"[^>]*>\s*Insights', html)


def test_the_inbox_badge_renders_once_per_nav(owner_client):
    html = owner_client.get("/dashboard/").content.decode()
    assert (
        html.count('hx-get="/inbox/badge/"') == 2
    )  # desktop + phone nav; only the shown one asks
    assert html.count('hx-get="/inbox/followups/badge/"') == 0


def test_the_inbox_badge_counts_what_is_mine(owner, owner_client):
    from apps.contacts.models import Contact
    from apps.conversations.models import Conversation

    user, account = owner
    other = User.objects.create_user("mate", "mate@example.com", "x")
    team = Team.objects.create(account=account, name="Sales")
    team.members.add(user)
    other_team = Team.objects.create(account=account, name="Support")

    def conv(**kw):
        contact = Contact.objects.create(
            account=account, phone=f"+2609{Contact.objects.count():08d}"
        )
        return Conversation.objects.create(
            account=account, contact=contact, channel="whatsapp", is_unread=True, **kw
        )

    conv()  # nobody's
    conv(assigned_to=user)  # mine
    conv(assigned_team=team)  # my team's, nobody picked
    conv(assigned_to=other)  # a teammate's
    conv(assigned_team=other_team)  # a team I'm not in
    conv(status=Conversation.Status.CLOSED)  # closed
    read = conv()
    Conversation.objects.filter(pk=read.pk).update(is_unread=False)  # already read
    html = owner_client.get("/inbox/badge/").content.decode()
    assert ">3<" in html and "3 unread conversations" in html


def test_an_empty_inbox_badge_is_empty(owner_client):
    assert owner_client.get("/inbox/badge/").content == b""


def test_inbox_settings_pages(owner_client):
    html = owner_client.get("/inbox/settings/teams/").content.decode()
    assert "Inbox settings" in html and 'id="teams"' in html and 'id="routing"' in html
    assert 'class="section-tabs ' not in html
    assert owner_client.get("/inbox/settings/").url == "/inbox/saved-replies/"
    assert owner_client.get("/inbox/teams/").url == "/inbox/settings/teams/#teams"
    assert (
        owner_client.get("/inbox/routing-rules/").url
        == "/inbox/settings/teams/#routing"
    )
    saved = owner_client.get("/inbox/saved-replies/").content.decode()
    assert "Teams &amp; routing" in saved


def test_adding_a_team_comes_back_to_teams_and_routing(owner, owner_client):
    resp = owner_client.post("/inbox/teams/", {"name": "Sales"})
    assert resp.url == "/inbox/settings/teams/#teams"
    assert Team.objects.filter(account=owner[1], name="Sales").exists()


def test_the_activity_log_is_a_settings_tab(owner_client):
    html = owner_client.get("/logs/messages/").content.decode()
    assert re.search(r'aria-current="page">Activity log<', html)


def test_ctrl_k_finds_every_page_and_old_names(owner_client):
    html = owner_client.get("/dashboard/").content.decode()
    raw = re.search(
        r'<script id="cmdk-pages" type="application/json">(.*?)</script>', html, re.S
    )
    items = json.loads(raw.group(1))
    labels = {i["label"] for i in items}
    for expected in (
        "Home \u203a Overview",
        "Inbox \u203a Follow-ups",
        "Customers \u203a All customers",
        "Settings \u203a Activity log",
    ):
        assert expected in labels
    old = {i["label"]: i["hint"] for i in items if i["hint"]}
    assert old["Message history"] == "now Settings \u203a Activity log"
    assert old["Billing & plans"] == "in your account menu"


def test_the_inbox_badge_stays_cheap(
    owner, owner_client, django_assert_max_num_queries
):
    """It's asked every few minutes by every open tab: a fixed handful of queries."""
    with django_assert_max_num_queries(12):
        owner_client.get("/inbox/badge/")


def test_the_sections_add_no_queries_to_a_page(owner_client, django_assert_num_queries):
    """The registry is pure Python on the request's context; rendering it twice (desktop
    and phone nav), the tab bar and Ctrl+K shares one build."""
    from django.template import Context, Template
    from django.test import RequestFactory

    request = RequestFactory().get("/contacts/")
    with django_assert_num_queries(0):
        html = Template(
            "{% load nav %}{% nav_sections as a %}{% nav_sections as b %}{{ a.current.label }}"
        ).render(Context({"request": request, **ALL_ON}))
    assert html == "Customers"
