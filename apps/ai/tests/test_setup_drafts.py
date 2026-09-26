"""AI setup drafts: AI proposes an intent or form fields, Akilent's own code checks and builds."""
import json

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings

from apps.accounts import profile
from apps.accounts.models import Account, Membership
from apps.ai.models import AIDraft, AISettings
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.tasks import build_draft
from apps.automation.models import Workflow
from apps.whatsapp.template_lint import lint


class Fake(AIProvider):
    answer, calls = "", []

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        Fake.calls.append({"system": system, "user": messages[-1].content})
        return CompletionResult(text=Fake.answer, model="fake-d")


@pytest.fixture(autouse=True)
def fake(settings, monkeypatch):
    settings.AI_PROVIDER_BACKEND = "ollama"
    monkeypatch.setattr("apps.ai.providers.get_ai_provider", lambda *a, **k: Fake())
    Fake.answer, Fake.calls = "", []
    cache.clear()


@pytest.fixture
def account(db):
    account = Account.objects.create(company_name="Sunrise Solar")
    AISettings.objects.create(account=account, enabled=True, business_notes="We install in one day.")
    profile.save_profile(account, location="Cairo Road, Lusaka")
    return account


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


def draft(account, kind, prompt="x", **context):
    d = AIDraft.objects.create(account=account, kind=kind, prompt=prompt, context=context)
    build_draft.apply(args=(d.pk,))
    d.refresh_from_db()
    return d


# ---- automation: an intent, never workflow structure ----
@pytest.mark.django_db
def test_a_description_becomes_an_intent_and_review_builds_it(owner, account):
    Fake.answer = json.dumps({"intent": "answer_question", "confidence": 0.9, "entities": {
        "keywords": ["installation", "install"], "reply_text": "We install in one day.",
        "topic_label": "installation", "steps": [{"type": "webhook"}]}})
    d = draft(account, "automation", "When people ask about installation, say we do it in one day")
    assert d.status == "ready" and d.result["intent"] == "answer_question"
    assert "Akilent builds the automation itself" in Fake.calls[0]["system"]
    page = owner.get(f"/build/review/?draft={d.pk}").content.decode()
    assert "Answer questions about installation" in page and "webhook" not in page
    owner.post("/build/review/", {"draft": d.pk, "action": "save"})
    assert Workflow.objects.get(slug="answer-installation").status == Workflow.Status.DRAFT
    d.refresh_from_db()
    assert d.status == "used"


@pytest.mark.django_db
@pytest.mark.parametrize("answer", [
    {"intent": "none", "confidence": 0},
    {"intent": "answer_pricing", "confidence": 0.3},
    {"intent": "run_a_webhook", "confidence": 0.99},
])
def test_unsure_or_unknown_becomes_a_clarifying_question(account, answer):
    Fake.answer = json.dumps(answer)
    d = draft(account, "automation", "do the thing")
    assert d.status == "error" and "couldn't tell what you want" in d.error


@pytest.mark.django_db
def test_a_pasted_conversation_is_masked_before_it_reaches_ai(owner, account, django_capture_on_commit_callbacks, monkeypatch):
    monkeypatch.setattr(build_draft, "apply_async", lambda *a, **k: None)
    with django_capture_on_commit_callbacks(execute=True):
        r = owner.post("/ai/drafts/", {"kind": "automation",
                                       "conversation": "Customer: call me on 0971234567\nUs: We're on Cairo Road."})
    d = AIDraft.objects.get(pk=r.json()["draft"]["id"])
    assert "0971234567" not in d.context["conversation"] and "[phone]" in d.context["conversation"]


# ---- templates ----
TEMPLATE = {"category": "utility", "name": "Thank You After Purchase!", "language": "en", "header": "",
            "body": "Hi {{1}}, thank you for your purchase. Please keep your receipt for any support.",
            "footer": "", "variables": [{"label": "Customer's first name", "example": "Mwila"}]}


_wa_urls = override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled", WHATSAPP_ENABLED=True)


@_wa_urls
@pytest.mark.django_db
def test_a_template_draft_fills_the_real_form_with_reasons(owner, account):
    Fake.answer = json.dumps(TEMPLATE)
    d = draft(account, "template", "Thank customers after they buy")
    assert d.status == "ready" and d.result["name"] == "thank_you_after_purchase"
    assert d.result["reasons"][0].startswith("Utility, because")
    assert any("first name" in r for r in d.result["reasons"])
    html = owner.get(f"/whatsapp/templates/new/?draft={d.pk}").content.decode()
    assert "thank_you_after_purchase" in html and "Why I built it this way" in html
    assert not Account.objects.get(pk=account.pk).whatsapp_numbers.exists(), "nothing submitted to Meta"


@pytest.mark.django_db
def test_an_unusable_template_draft_is_an_error_not_a_form(account):
    Fake.answer = json.dumps(dict(TEMPLATE, body="Hi {{2}}, thanks.", variables=[]))
    d = draft(account, "template", "x")
    assert d.status == "error" and "wasn't usable" in d.error


@pytest.mark.django_db
def test_an_edit_must_keep_the_blanks(account):
    body = "Hi {{1}}, your order {{2}} is ready."
    Fake.answer = json.dumps({"body": "Hello {{1}}! Great news: order {{2}} is ready to collect."})
    ok = draft(account, "template_edit", "friendlier", body=body, instruction="friendlier", category="utility", language="en")
    assert ok.status == "ready" and ok.result["after"].startswith("Hello {{1}}")
    assert ["add", "Hello"] in ok.result["diff"] or any(p[0] == "add" for p in ok.result["diff"])
    Fake.answer = json.dumps({"body": "Hello, your order is ready."})
    dropped = draft(account, "template_edit", "shorter", body=body, instruction="shorter")
    assert dropped.status == "error" and "changed the blanks" in dropped.error


@pytest.mark.django_db
def test_translating_to_an_unsupported_language_warns(account):
    Fake.answer = json.dumps({"body": "Mwapoleni {{1}}, oda yenu {{2}} yaisa."})
    d = draft(account, "template_edit", "translate", body="Hi {{1}}, order {{2}} is here.",
              instruction="translate:bem", category="utility", language="en")
    assert d.status == "ready" and any("Bemba" in w for w in d.warnings)


@pytest.mark.parametrize("fields,words", [
    ({"body": "{{1}}, your order is ready."}, "starts with a blank"),
    ({"body": "Your order is ready {{1}}"}, "ends with a blank"),
    ({"body": "Hi {{1}} {{2}}, your order is ready now."}, "next to each other"),
    ({"body": "Hi {{1}} {{2}} {{3}}."}, "a lot of blanks"),
    ({"body": "Get 20% off, a special discount today!", "category": "utility"}, "sound like marketing"),
    ({"body": "Your order is ready.", "language": "bem"}, "Bemba"),
])
def test_lint_catches_likely_rejections(fields, words):
    args = {"category": "utility", "language": "en", **fields}
    assert any(words in w["text"] for w in lint(**args))


def test_a_clean_utility_template_has_no_warnings():
    assert lint(category="utility", language="en", body="Hi {{1}}, your order {{2}} is ready to collect today.") == []


@_wa_urls
@pytest.mark.django_db
def test_drafts_need_ai_on_and_stay_in_their_business(owner, account):
    other = Account.objects.create(company_name="Other")
    theirs = AIDraft.objects.create(account=other, kind="template", prompt="x", status="ready", result=TEMPLATE)
    assert owner.get(f"/ai/drafts/{theirs.pk}/").status_code == 404
    assert "Why I built it" not in owner.get(f"/whatsapp/templates/new/?draft={theirs.pk}").content.decode()
    AISettings.objects.filter(account=account).update(enabled=False)
    assert owner.post("/ai/drafts/", {"kind": "template", "prompt": "x"}).status_code == 400
