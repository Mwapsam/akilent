"""Ad-aware automations (release flag ``ads_automation``, docs/strategy/akilent-ads.md).

``trigger.from_ad`` answers the first message of a chat someone started by tapping a Meta
ad. Later messages in that chat, and chats from anywhere else, are not "from an ad".
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.automation import explain
from apps.automation.engagement_starters import (
    AD_REPLY_PRIORITY,
    STARTERS_BY_KEY,
    build_reply_definition,
)
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.template_render import render_template
from apps.automation.workflow_engine import (
    explain_enrollment,
    trigger_verdict,
    validate_definition,
)
from apps.conversations.tests.test_ad_referrals import (  # noqa: F401 (fixtures)
    WA_AD,
    account,
    deliver,
)

pytestmark = pytest.mark.django_db

FLAG = "ads_automation"


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({FLAG})


def _wf(account, trigger, name="Ad reply", priority=100):
    return Workflow.objects.create(
        account=account,
        name=name,
        slug=name.lower().replace(" ", "-"),
        status=Workflow.Status.PUBLISHED,
        priority=priority,
        definition={
            "trigger": trigger,
            "steps": [
                {"id": "tag", "type": "add_tag", "tag": "from-ad", "next": "stop"},
                {"id": "stop", "type": "stop"},
            ],
        },
    )


def _runs(wf):
    return WorkflowRun.objects.filter(workflow=wf).count()


# --- Validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger,ok",
    [
        ({"type": "conversation.message_received", "from_ad": True}, True),
        ({"type": "conversation.message_received", "from_ad": ["1202"]}, True),
        ({"type": "conversation.message_received", "from_ad": []}, False),
        ({"type": "conversation.message_received", "from_ad": "yes"}, False),
        # whatsapp.received starts before the ad is read off the message.
        ({"type": "whatsapp.received", "from_ad": True}, False),
        ({"type": "lead.created", "from_ad": True}, False),
    ],
)
def test_from_ad_validation(trigger, ok):
    errors = [
        e
        for e in validate_definition({"trigger": trigger, "steps": [{"id": "s", "type": "stop"}]})
        if e["field"] == "trigger.from_ad"
    ]
    assert (not errors) is ok


def test_verdict_codes():
    wf = Workflow(name="x")
    any_ad = {"type": "conversation.message_received", "from_ad": True}
    some = {"type": "conversation.message_received", "from_ad": ["A"]}
    assert trigger_verdict(wf, any_ad, None, {"message": {}})["code"] == "not_from_ad"
    assert trigger_verdict(wf, any_ad, None, {"ad": {"source_id": "B"}}) is None
    assert trigger_verdict(wf, some, None, {"ad": {"source_id": "B"}})["code"] == "other_ad"
    assert trigger_verdict(wf, some, None, {"ad": {"source_id": "A"}}) is None


def test_explanations_have_owner_wording():
    wf = Workflow(name="Ad reply")
    for code in ("not_from_ad", "other_ad"):
        text = explain.describe_verdict({"workflow": wf, "code": code, "details": {}})
        assert "Meta ad" in text["headline"]


# --- The real inbound path ------------------------------------------------------------------


def test_fires_on_the_ad_message_only(account, deliver, released):
    wf = _wf(account, {"type": "conversation.message_received", "from_ad": True})
    _contact, send = deliver

    send("Hi, is this still available?", referral=WA_AD)
    assert _runs(wf) == 1
    send("And in size 8?")  # same ad chat, later message
    assert _runs(wf) == 1


def test_a_chat_not_from_an_ad_never_fires(account, deliver, released):
    wf = _wf(account, {"type": "conversation.message_received", "from_ad": True})
    _contact, send = deliver
    send("Hello")
    assert _runs(wf) == 0


def test_only_the_listed_ads(account, deliver, released):
    other = _wf(
        account,
        {"type": "conversation.message_received", "from_ad": ["999"]},
        name="Other ad",
    )
    mine = _wf(
        account,
        {"type": "conversation.message_received", "from_ad": [WA_AD["source_id"]]},
        name="This ad",
    )
    _contact, send = deliver
    send("Hi", referral=WA_AD)
    assert (_runs(other), _runs(mine)) == (0, 1)


def test_unreleased_a_saved_ad_workflow_never_matches(account, deliver, settings):
    settings.RELEASED_FEATURES = frozenset()
    wf = _wf(account, {"type": "conversation.message_received", "from_ad": True})
    _contact, send = deliver
    send("Hi", referral=WA_AD)
    assert _runs(wf) == 0


def test_ad_reply_outranks_the_greeting_so_one_answer(account, deliver, released):
    """Both talk; the ad starter's priority puts it first, and only one answers."""
    from unittest.mock import patch

    greet = Workflow.objects.create(
        account=account,
        name="Greet",
        slug="greet",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "conversation.message_received"},
            "steps": [
                {"id": "r", "type": "reply_text", "text": "Hello!", "next": "stop"},
                {"id": "stop", "type": "stop"},
            ],
        },
    )
    starter = STARTERS_BY_KEY["answer-ad-chats"]
    ad = Workflow.objects.create(
        account=account,
        name=starter["name"],
        slug=starter["key"],
        status=Workflow.Status.PUBLISHED,
        priority=AD_REPLY_PRIORITY,
        definition=build_reply_definition(starter, text="Thanks for tapping our ad!"),
    )
    _contact, send = deliver
    with patch("apps.automation.workflow_engine._answered", return_value=True):
        send("Hi", referral=WA_AD)
    assert (_runs(ad), _runs(greet)) == (1, 0)


def test_explain_enrollment_sees_the_ad(account, released):
    from apps.contacts.models import Contact

    contact = Contact.objects.create(account=account, phone="+260970000001")
    _wf(account, {"type": "conversation.message_received", "from_ad": ["A"]})
    from django.utils import timezone

    [no_ad] = explain_enrollment(
        account.id, contact, message={"body": "hi"}, message_at=timezone.now(),
        is_first_message=True,
    )
    [other] = explain_enrollment(
        account.id, contact, message={"body": "hi"}, message_at=timezone.now(),
        is_first_message=True, ad={"source_id": "B"},
    )
    assert (no_ad["code"], other["code"]) == ("not_from_ad", "other_ad")


# --- Reply text and the starter ---------------------------------------------------------------


def test_ad_headline_placeholder_renders():
    assert (
        render_template("About {ad.headline}?", {"ad.headline": "Summer dresses"})
        == "About Summer dresses?"
    )


def test_ad_starter_definition_is_valid_and_has_no_keywords():
    starter = STARTERS_BY_KEY["answer-ad-chats"]
    definition = build_reply_definition(starter, text="Thanks!", notify=True)
    assert definition["trigger"] == {
        "type": "conversation.message_received",
        "from_ad": True,
    }
    assert [s["type"] for s in definition["steps"]] == ["reply_text", "notify_team", "stop"]
    assert not [e for e in validate_definition(definition) if e["severity"] == "error"]


def _owner_client(account):
    from django.contrib.auth.models import User
    from django.test import Client

    user = User.objects.create_user("owner", "owner@x.example", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return client


def test_starter_hidden_and_refused_until_released(account, settings):
    settings.RELEASED_FEATURES = frozenset()
    client = _owner_client(account)
    html = client.get(reverse("automation:list")).content.decode()
    assert "answer-ad-chats" not in html
    client.post(
        reverse("automation:starter-install"),
        {"starter": "answer-ad-chats", "reply_text": "Hi!"},
    )
    assert not Workflow.objects.filter(account=account, slug="answer-ad-chats").exists()


def test_starter_installs_first_in_line_when_released(account, released):
    client = _owner_client(account)
    assert "answer-ad-chats" in client.get(reverse("automation:list")).content.decode()
    client.post(
        reverse("automation:starter-install"),
        {"starter": "answer-ad-chats", "reply_text": "Thanks for tapping our ad!"},
    )
    wf = Workflow.objects.get(account=account, slug="answer-ad-chats")
    assert wf.status == Workflow.Status.PUBLISHED
    assert wf.priority == AD_REPLY_PRIORITY
    assert wf.definition["trigger"]["from_ad"] is True


def test_editor_offers_the_ad_filter_only_when_released(account, settings):
    from django.utils import timezone

    from apps.contacts.models import Contact
    from apps.conversations.models import AdReferral, Conversation

    contact = Contact.objects.create(account=account, phone="+260970000002")
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )
    AdReferral.objects.create(
        account=account,
        conversation=conversation,
        channel=AdReferral.Channel.WHATSAPP,
        source_type="ad",
        source_id="777",
        headline="</script><b>Sale",
        received_at=timezone.now(),
    )
    wf = _wf(account, {"type": "conversation.message_received"})
    client = _owner_client(account)
    url = reverse("automation:editor", args=[wf.slug])

    settings.RELEASED_FEATURES = frozenset()
    assert "data-ad-filter" not in client.get(url).content.decode()

    settings.RELEASED_FEATURES = frozenset({FLAG})
    html = client.get(url).content.decode()
    assert "data-ad-filter" in html
    assert r"\u003c/script>\u003cb>Sale" in html and "</script><b>Sale" not in html
