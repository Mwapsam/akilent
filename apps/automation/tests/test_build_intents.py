"""Build with AI: every intent becomes a real, valid workflow, decided by code, not by a model."""
import pytest

from apps.accounts import business_hours, profile
from apps.accounts.models import Account
from apps.automation import api as automation_api
from apps.automation.intents import INTENTS, IntentError, build_from_intent
from apps.automation.models import Workflow
from apps.automation.workflow_engine import validate_definition


@pytest.fixture
def account(db):
    account = Account.objects.create(company_name="Sunrise Solar")
    profile.save_profile(account, what_you_sell="Solar systems", location="Plot 12, Cairo Road, Lusaka",
                         delivers=True, delivery_notes="Lusaka next day", payment_methods=["mtn_momo", "cash"])
    business_hours.save_hours(account, tz="Africa/Lusaka", schedule={
        d: {"open": "08:00", "close": "17:00"} for d in ("mon", "tue", "wed", "thu", "fri")})
    return account


ENTITIES = {
    "answer_question": {"keywords": ["installation"], "reply_text": "Installation takes one day.", "topic_label": "installation"},
    "offer_menu": {},
}


@pytest.mark.django_db
@pytest.mark.parametrize("intent", [k for k in INTENTS if k != "follow_up_quiet"])
def test_every_intent_builds_a_workflow_the_engine_accepts(account, intent):
    built = build_from_intent(account, intent, ENTITIES.get(intent, {}))
    assert built["errors"] == [], built["errors"]
    assert [e for e in validate_definition(built["definition"], account=account) if e["severity"] != "warning"] == []
    assert built["reasons"], "every draft explains itself"


@pytest.mark.django_db
def test_the_model_can_only_name_an_intent_and_a_few_words(account):
    built = build_from_intent(account, "answer_pricing", {
        "keywords": ["Price", "price", "cost"], "reply_text": "From K18,000.",
        "steps": [{"type": "webhook", "url": "http://evil"}], "trigger": {"type": "manual"}})
    assert built["definition"]["trigger"]["match"]["any"] == ["price", "cost"]
    assert "webhook" not in str(built["definition"]) and built["definition"]["trigger"]["type"] == "conversation.message_received"
    with pytest.raises(IntentError):
        build_from_intent(account, "delete_all_customers", {})


@pytest.mark.django_db
def test_replies_use_the_owners_answers_and_warn_about_unchecked_prices(account):
    built = build_from_intent(account, "answer_location", {})
    assert built["reply_text"] == "We're at {location}." and built["preview"] == "We're at Plot 12, Cairo Road, Lusaka."
    assert any("stays up to date" in r for r in built["reasons"])
    priced = build_from_intent(account, "answer_pricing", {"reply_text": "It's K5,000 installed."})
    assert any("K5,000" in w for w in priced["warnings"])


@pytest.mark.django_db
def test_an_answer_needs_the_fact_it_quotes(db):
    bare = Account.objects.create(company_name="New")
    with pytest.raises(IntentError, match="where you are"):
        build_from_intent(bare, "answer_location", {})


@pytest.mark.django_db
def test_a_keyword_clash_with_a_live_automation_warns(account):
    live = build_from_intent(account, "answer_pricing", {})
    automation_api.save_built_workflow(account, slug="prices-live", name="My prices", definition=live["definition"], turn_on=True)
    again = build_from_intent(account, "answer_question", {"keywords": ["price"], "reply_text": "Ask us."})
    assert any("My prices" in w for w in again["warnings"])


@pytest.mark.django_db
def test_save_as_draft_never_touches_a_live_automation(account):
    built = build_from_intent(account, "answer_location", {})
    on = automation_api.save_built_workflow(account, slug=built["slug"], name=built["name"],
                                            definition=built["definition"], turn_on=True)
    assert on.status == Workflow.Status.PUBLISHED
    draft = automation_api.save_built_workflow(account, slug=built["slug"], name=built["name"],
                                               definition=built["definition"])
    assert draft.status == Workflow.Status.DRAFT and draft.slug != on.slug
    on.refresh_from_db()
    assert on.status == Workflow.Status.PUBLISHED


@pytest.mark.django_db
def test_follow_up_needs_an_approved_template(account):
    with pytest.raises(IntentError, match="approved WhatsApp template"):
        build_from_intent(account, "follow_up_quiet", {})
