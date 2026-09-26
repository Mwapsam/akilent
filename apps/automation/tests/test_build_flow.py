"""Build: the interview, recommendations from real data, repeated replies, the review page."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts import business_hours, profile
from apps.accounts.models import Account, Membership
from apps.automation import patterns, recommendations
from apps.automation.models import ReplyPattern, Workflow
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

NOW = timezone.now()


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Sunrise Solar")


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


def convo(account, n):
    contact = Contact.objects.create(account=account, phone=f"+26097100{n:04d}", first_name=f"C{n}")
    return Conversation.objects.create(account=account, contact=contact, channel="email", last_message_at=NOW)


def say(c, body, *, out=False, minutes_ago=0, meta=None):
    return Message.objects.create(account=c.account, conversation=c, body=body,
                                  direction=Message.Direction.OUTBOUND if out else Message.Direction.INBOUND,
                                  timestamp=NOW - timedelta(minutes=minutes_ago), status="sent", metadata=meta or {})


# ---- step 1: the interview ----
@pytest.mark.django_db
def test_the_interview_saves_the_profile_hours_and_starts_the_ai_notes(owner, account):
    from apps.ai.models import AISettings

    r = owner.post("/build/profile/", {
        "what_you_sell": "Solar systems", "location": "Cairo Road, Lusaka", "delivers": "yes",
        "delivery_notes": "Lusaka next day", "payment_methods": ["mtn_momo", "cash", "bitcoin"],
        "website": "sunrise.example", "set_hours": "on", "timezone": "Africa/Lusaka",
        "mon_open": "on", "mon_from": "08:00", "mon_to": "17:00",
    })
    assert r.status_code == 302 and r["Location"].endswith("?step=2")
    p = profile.get_profile(account)
    assert (p.payment_methods, p.website, p.delivers) == (["cash", "mtn_momo"], "https://sunrise.example", True)
    assert business_hours.describe(account) == "Monday 08:00-17:00"
    notes = AISettings.objects.get(account=account).business_notes
    assert "Where we are: Cairo Road, Lusaka" in notes and "Payment: Cash or MTN MoMo" in notes
    assert AISettings.objects.get(account=account).enabled is False, "the interview never switches AI on"


@pytest.mark.django_db
def test_profile_answers_fill_replies_and_template_blanks(account):
    from apps.automation import variables

    profile.save_profile(account, location="Cairo Road", payment_methods=["airtel_money"])
    assert variables.merge_business_facts("We're at {location}. Pay by {payment_methods}. {website}", account) == \
        "We're at Cairo Road. Pay by Airtel Money. {website}"
    assert variables.read_source("business.location", contact=None, account=account, context={}) == "Cairo Road"
    assert not variables.is_fixed_text("business.location")


@pytest.mark.django_db
def test_autopilot_topics_unlock_with_profile_answers(account):
    from apps.ai import autonomy

    assert {"location", "payment"} <= set(autonomy.locked_topics(account))
    profile.save_profile(account, location="Cairo Road", payment_methods=["cash"])
    assert not {"location", "payment"} & set(autonomy.locked_topics(account))


# ---- step 3: recommendations ----
@pytest.mark.django_db
def test_recommendations_come_from_real_numbers_and_skip_whats_already_on(account):
    for n in range(4):
        c = convo(account, n)
        say(c, "How much is the 3kW system?", minutes_ago=30)
        say(c, "hello", out=True, minutes_ago=2)            # answered, after a 28 minute wait
    cards = recommendations.recommend(account)
    pricing = next(c for c in cards if c["intent"] == "answer_pricing")
    assert pricing["evidence"].startswith("4 customers asked about prices")
    welcome = next(c for c in cards if c["intent"] == "welcome_new")
    assert "wait 28 minutes" in welcome["evidence"]
    Workflow.objects.create(account=account, slug="answer-pricing-questions", name="Prices",
                            definition={}, status=Workflow.Status.PUBLISHED)
    assert "answer_pricing" not in [c["intent"] for c in recommendations.recommend(account)]


@pytest.mark.django_db
def test_after_hours_messages_recommend_replying_when_closed(account):
    business_hours.save_hours(account, tz="UTC", schedule={d: {"open": "00:00", "close": "00:01"}
                                                           for d in business_hours.DAYS})
    for n in range(3):
        say(convo(account, n), "anyone there?", minutes_ago=10 + n)
    card = next(c for c in recommendations.recommend(account) if c["intent"] == "reply_when_closed")
    assert card["evidence"].startswith("100% of customer messages arrive")


@pytest.mark.django_db
def test_a_new_business_gets_cards_from_its_answers_and_dismissed_ones_stay_away(account):
    profile.save_profile(account, location="Cairo Road")
    intents = [c["intent"] for c in recommendations.recommend(account)]
    assert "answer_location" in intents and "welcome_new" in intents and len(intents) <= 3
    patterns.dismiss(account, "intent:answer_location")
    assert "answer_location" not in [c["intent"] for c in recommendations.recommend(account)]


# ---- repeated replies ----
def repeated(account, n=4, reply="We install in one day and installation is free within Lusaka.", meta=None, start=100):
    for i in range(n):
        c = convo(account, start + i)
        say(c, f"How long does installation take for house {i}?", minutes_ago=60)
        say(c, reply, out=True, minutes_ago=50, meta=meta)


@pytest.mark.django_db
def test_the_team_s_repeated_reply_becomes_an_offer(account):
    repeated(account)
    assert patterns.refresh(account) == 1
    pattern = ReplyPattern.objects.get()
    assert pattern.count == 4 and "installation" in pattern.question_keywords
    assert pattern.reply_text.startswith("We install in one day")


@pytest.mark.django_db
def test_ai_and_automation_replies_and_short_ones_are_not_patterns(account):
    repeated(account, meta={"sent_by": "automation"})
    repeated(account, meta={"sent_by": "ai"}, start=200)
    repeated(account, reply="ok thanks", start=300)
    assert patterns.refresh(account) == 0


@pytest.mark.django_db
def test_the_inbox_offers_to_automate_and_review_builds_it(owner, account):
    repeated(account)
    patterns.refresh(account)
    pattern = ReplyPattern.objects.get()
    c = Conversation.objects.get(pk=Message.objects.get(pk=pattern.message_ids[0]).conversation_id)
    html = owner.get(f"/inbox/{c.public_id}/").content.decode()
    assert f"/build/review/?pattern={pattern.key}" in html

    page = owner.get(f"/build/review/?pattern={pattern.key}").content.decode()
    assert "Reuses the reply your team has sent 4 times." in page and "Why I built it this way" in page
    r = owner.post("/build/review/", {"pattern": pattern.key, "action": "turn_on",
                                      "reply_text": pattern.reply_text, "keywords": "installation", "tag": "asked-installation"})
    assert r.status_code == 302
    wf = Workflow.objects.get(slug="answer-installation")
    assert wf.status == Workflow.Status.PUBLISHED and wf.definition["trigger"]["match"]["any"] == ["installation"]
    assert f"/build/review/?pattern={pattern.key}" not in owner.get(f"/inbox/{c.public_id}/").content.decode(), \
        "once automated, the offer goes away"


@pytest.mark.django_db
def test_why_didnt_it_reply_offers_to_automate_a_common_question(owner, account):
    repeated(account)
    patterns.refresh(account)
    c = convo(account, 900)
    say(c, "how long is installation?")
    assert "Automate it" in owner.get(f"/automations/why-not/?conversation={c.public_id}").content.decode()


# ---- the review page ----
@pytest.mark.django_db
def test_save_as_draft_leaves_it_off_and_turn_on_publishes(owner, account):
    profile.save_profile(account, location="Cairo Road")
    page = owner.get("/build/review/?intent=answer_location").content.decode()
    assert "We&#x27;re at Cairo Road." in page and "Nothing has been saved yet" in page
    owner.post("/build/review/", {"intent": "answer_location", "action": "save"})
    assert Workflow.objects.get(slug="answer-where-are-you").status == Workflow.Status.DRAFT
    owner.post("/build/review/", {"intent": "answer_location", "action": "turn_on"})
    assert Workflow.objects.get(slug="answer-where-are-you").status == Workflow.Status.PUBLISHED


@pytest.mark.django_db
def test_build_pages_render_without_ai(owner, account):
    for step in (1, 2, 3):
        html = owner.get(f"/build/?step={step}").content.decode()
        assert "Build" in html and "Describe something else" not in html


@pytest.mark.django_db
def test_another_business_pattern_is_not_reachable(owner, account):
    other = Account.objects.create(company_name="Other")
    repeated(other)
    patterns.refresh(other)
    key = ReplyPattern.objects.get(account=other).key
    r = owner.get(f"/build/review/?pattern={key}")
    assert r.status_code == 302 and not Workflow.objects.filter(account=account).exists()
