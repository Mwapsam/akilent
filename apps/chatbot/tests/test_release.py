"""Chatbot Studio P6 (docs/plans/chatbot-studio.md): selling and releasing website chat.

- ``website_chat`` is a plan feature sold only on the plans an operator ticks: new plans don't
  start with it, and plan cards hide it until ``chatbot`` is released.
- Chatbot pages need the feature; so does a *new* widget chat. A chat already under way carries
  on (a plan change never cuts a visitor off mid-conversation).
- The help guide, the API docs section and the integration page appear only once released.
"""

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import Membership
from apps.billing.models import AccountFeatureOverride, Plan, PlanFeature
from apps.chatbot.models import ChatSession

ORIGIN = "https://acme.com"
pytestmark = pytest.mark.django_db


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def _init(chatbot, session_key=None):
    body = {"public_key": chatbot.public_key}
    if session_key:
        body["session_key"] = session_key
    return _post(Client(HTTP_ORIGIN=ORIGIN), "/api/chat/init/", body)


def _grant(account):
    AccountFeatureOverride.objects.create(
        account=account, key="website_chat", grant=True, note="pilot"
    )


@pytest.fixture
def owner(account):
    user = get_user_model().objects.create_user("o", "o@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return client


# ── The plan feature ────────────────────────────────────────────────────────────


def test_new_plans_dont_start_with_website_chat(db):
    plan = Plan.objects.create(slug="growth", name="Growth", price_monthly=Decimal(29))
    assert not PlanFeature.objects.filter(plan=plan, key="website_chat").exists()


def test_plan_cards_hide_it_until_released(settings):
    from apps.core.channels import feature_visible

    settings.RELEASED_FEATURES = frozenset()
    assert feature_visible("website_chat", {"whatsapp"}) is False
    settings.RELEASED_FEATURES = frozenset({"chatbot"})
    assert feature_visible("website_chat", {"whatsapp"}) is True


@pytest.mark.real_plan
class TestPlanGate:
    def test_pages_are_locked_without_the_feature(self, owner, chatbot, settings):
        settings.RELEASED_FEATURES = frozenset({"chatbot"})
        resp = owner.get("/chatbot/")
        assert resp.status_code == 302
        assert resp["Location"].endswith("/billing/locked/website_chat/")
        assert owner.get(f"/chatbot/{chatbot.pk}/").status_code == 302

    def test_pages_open_with_a_grant(self, owner, chatbot, account, settings):
        settings.RELEASED_FEATURES = frozenset({"chatbot"})
        _grant(account)
        assert owner.get("/chatbot/").status_code == 200

    def test_no_new_chats_without_the_feature(self, chatbot):
        resp = _init(chatbot)
        assert resp.status_code == 403
        assert not ChatSession.objects.exists()

    def test_new_chats_with_a_plan_that_includes_it(self, chatbot, account):
        from apps.billing.models import Subscription

        plan = Plan.objects.create(slug="pro", name="Pro", price_monthly=Decimal(49))
        PlanFeature.objects.create(plan=plan, key="website_chat")
        from django.utils import timezone

        Subscription.objects.create(
            account=account,
            plan=plan,
            status=Subscription.ACTIVE,
            current_period_start=timezone.now(),
        )
        assert _init(chatbot).status_code == 200

    def test_a_chat_under_way_carries_on_after_the_plan_changes(self, chatbot, account):
        _grant(account)
        key = _init(chatbot).json()["session_key"]
        AccountFeatureOverride.objects.filter(account=account).delete()
        resumed = _init(chatbot, key)
        assert resumed.status_code == 200
        assert resumed.json()["session_key"] == key


# ── Released pages ──────────────────────────────────────────────────────────────


class TestReleasedPages:
    def test_help_guide_waits_for_the_release(self, settings):
        from apps.core.help import get_article

        settings.RELEASED_FEATURES = frozenset()
        assert get_article("website-chat") is None
        settings.RELEASED_FEATURES = frozenset({"chatbot"})
        assert get_article("website-chat") is not None

    def test_help_page_renders(self, client, settings):
        settings.RELEASED_FEATURES = frozenset({"chatbot"})
        page = client.get("/help/website-chat/").content.decode()
        assert "Talk to a person" in page and "Get it live" in page

    def test_integration_page_waits_for_the_release(self, client, settings):
        settings.RELEASED_FEATURES = frozenset()
        assert client.get("/integrations/website-chat/").status_code == 404
        settings.RELEASED_FEATURES = frozenset({"chatbot"})
        page = client.get("/integrations/website-chat/")
        assert page.status_code == 200
        assert "hands the chat to your team" in page.content.decode()

    def test_api_docs_section_waits_for_the_release(self, client, settings):
        settings.RELEASED_FEATURES = frozenset()
        assert 'id="website-chat"' not in client.get("/docs/channels/").content.decode()
        settings.RELEASED_FEATURES = frozenset({"chatbot"})
        assert 'id="website-chat"' in client.get("/docs/channels/").content.decode()
