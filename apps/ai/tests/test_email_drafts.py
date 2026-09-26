"""AI help for email templates: the model writes words, Akilent builds the email and the owner saves it."""
import json

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache

from apps.accounts.models import Account, Membership
from apps.ai.models import AIDraft, AISettings
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.tasks import build_draft
from apps.email import ai_layout
from apps.email.models import EmailTemplate
from apps.email.services.render import render_template


class Fake(AIProvider):
    answer, calls = "", []

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        Fake.calls.append({"system": system, "user": messages[-1].content})
        return CompletionResult(text=Fake.answer, model="fake-e")


@pytest.fixture(autouse=True)
def fake(settings, monkeypatch):
    settings.AI_PROVIDER_BACKEND = "ollama"
    monkeypatch.setattr("apps.ai.providers.get_ai_provider", lambda *a, **k: Fake())
    monkeypatch.setattr("apps.billing.limits.LimitChecker.has_feature", lambda self, name: True)
    Fake.answer, Fake.calls = "", []
    cache.clear()


@pytest.fixture
def account(db):
    account = Account.objects.create(company_name="Sunrise Solar")
    AISettings.objects.create(account=account, enabled=True, business_notes="We install in one day.")
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


RECEIPT = {
    "name": "Payment receipt", "subject": "Thanks for your payment, {{ first_name }}",
    "preheader": "Your receipt {{ receipt_number }}", "heading": "Payment received",
    "paragraphs": ["Hi {{ first_name }}, thank you for paying {{ company_name }}.",
                   "Your receipt number is {{ receipt_number }}."],
    "button": {"label": "View receipt", "url_variable": "receipt_url"},
    "sign_off": "The Sunrise Solar team",
    "variables": [{"name": "first_name", "example": "Mwila"}, {"name": "receipt_number", "example": "R-104"},
                  {"name": "receipt_url", "example": "https://sunrise.example/r/104"}],
}


# ---- the draft: parts in, Akilent-built email out ----
@pytest.mark.django_db
def test_a_description_becomes_a_built_email_with_reasons(account):
    Fake.answer = json.dumps(RECEIPT)
    d = draft(account, "email_template", "Thank customers after they pay")
    assert d.status == "ready", d.error
    fields = d.result["fields"]
    assert fields["subject"] == "Thanks for your payment, {{ first_name }}"
    assert '<a href="{{ receipt_url }}"' in fields["html_body"] and "{%" not in fields["html_body"]
    assert fields["sample_variables"]["first_name"] == "Mwila"
    assert fields["sample_variables"]["receipt_url"] == "https://example.com", "never a link the model typed"
    assert any("first_name" in r for r in d.result["reasons"])
    assert any("receipt_url" in r for r in d.result["reasons"])
    assert "Akilent builds the email's design" in Fake.calls[0]["system"]


@pytest.mark.django_db
def test_the_built_email_renders_with_its_sample_variables(account):
    Fake.answer = json.dumps(RECEIPT)
    fields = draft(account, "email_template").result["fields"]
    template = EmailTemplate(account=account, subject=fields["subject"], text_body=fields["text_body"],
                             html_body=fields["html_body"])
    subject, text, html = render_template(template, fields["sample_variables"])
    assert subject == "Thanks for your payment, Mwila"
    assert "R-104" in text and "Sunrise Solar" in html and "{{" not in html


@pytest.mark.django_db
def test_code_markup_and_links_from_the_model_never_reach_the_email(account):
    Fake.answer = json.dumps(dict(RECEIPT, heading='<script>alert(1)</script>{% load static %}Hello',
                                  paragraphs=["Visit https://evil.example now {# hidden #}", "<b>Bold</b> & more"],
                                  button={"label": "Pay", "url_variable": "not_listed"}))
    d = draft(account, "email_template")
    assert d.status == "ready", d.error
    html = d.result["fields"]["html_body"]
    assert "<script" not in html and "{% load" not in html and "evil.example" not in html and "{#" not in html
    assert "&amp; more" in html and "<b>" not in html
    assert d.result["parts"]["button"] is None, "a button must link to a listed blank"


@pytest.mark.django_db
@pytest.mark.parametrize("change", [
    {"subject": "Hi {{ customer.password }}"},
    {"paragraphs": ["Your total is {{ total|safe }}."]},
    {"paragraphs": ["Hi {{ first_name }"]},
    {"subject": ""},
    {"paragraphs": []},
])
def test_an_unusable_email_draft_is_an_error_not_a_form(account, change):
    Fake.answer = json.dumps(dict(RECEIPT, **change))
    d = draft(account, "email_template")
    assert d.status == "error" and "wasn't usable" in d.error


@pytest.mark.django_db
def test_an_invented_price_is_flagged(account):
    Fake.answer = json.dumps(dict(RECEIPT, paragraphs=["Installation now costs K4,500 only."]))
    d = draft(account, "email_template")
    assert d.status == "ready" and any("K4,500" in w for w in d.warnings)


# ---- rewrites keep the blanks ----
@pytest.mark.django_db
def test_a_rewrite_keeps_the_blanks_and_rebuilds_the_email(account):
    parts = ai_layout.clean_parts(RECEIPT)
    Fake.answer = json.dumps({"subject": "Payment received, {{ first_name }}!", "preheader": "Receipt {{ receipt_number }}",
                              "heading": "Thank you!", "button_label": "See receipt", "sign_off": "Sunrise Solar",
                              "paragraphs": ["Thanks {{ first_name }} for paying {{ company_name }}.",
                                             "Receipt: {{ receipt_number }}."]})
    d = draft(account, "email_edit", "friendlier", parts=parts, instruction="friendlier")
    assert d.status == "ready", d.error
    assert d.result["fields"]["subject"] == "Payment received, {{ first_name }}!"
    assert '<a href="{{ receipt_url }}"' in d.result["fields"]["html_body"], "the link can't be changed by a rewrite"
    assert any(op == "add" for op, _ in d.result["diff"])


@pytest.mark.django_db
def test_a_rewrite_that_drops_a_blank_is_refused(account):
    parts = ai_layout.clean_parts(RECEIPT)
    Fake.answer = json.dumps({"subject": "Payment received", "paragraphs": ["Thanks for paying."]})
    d = draft(account, "email_edit", "shorter", parts=parts, instruction="shorter")
    assert d.status == "error" and "changed the blanks" in d.error


@pytest.mark.django_db
def test_subject_lines_only_use_blanks_the_email_has(account):
    Fake.answer = json.dumps({"subjects": ["Your receipt, {{ first_name }}", "Hi {{ unknown }}",
                                           "Thanks from {{ company_name }}", "Payment received", "A fourth one"]})
    d = draft(account, "email_edit", "subjects", instruction="subjects",
              subject="Receipt", text_body="Hi {{ first_name }}, thanks.", html_body="")
    assert d.status == "ready"
    assert d.result["subjects"] == ["Your receipt, {{ first_name }}", "Thanks from {{ company_name }}",
                                    "Payment received"]


# ---- the pages ----
@pytest.mark.django_db
def test_a_draft_fills_the_create_form_and_creating_marks_it_used(owner, account):
    Fake.answer = json.dumps(RECEIPT)
    d = draft(account, "email_template")
    page = owner.get(f"/email/templates/?draft={d.pk}").content.decode()
    assert "Why I built it this way" in page and 'value="Payment receipt"' in page
    fields = d.result["fields"]
    owner.post("/email/templates/create/", {
        "name": fields["name"], "subject": fields["subject"], "text_body": fields["text_body"],
        "html_body": fields["html_body"], "sample_variables": json.dumps(fields["sample_variables"]), "draft": d.pk})
    saved = EmailTemplate.objects.filter(account=account, name="Payment receipt").latest("pk")  # a starter shares the name
    assert saved.sample_variables["receipt_number"] == "R-104"
    d.refresh_from_db()
    assert d.status == "used"


@pytest.mark.django_db
def test_the_endpoint_rechecks_parts_from_the_browser(owner, account, monkeypatch):
    monkeypatch.setattr(build_draft, "apply_async", lambda *a, **k: None)
    bad = dict(RECEIPT, heading="{% debug %}")
    r = owner.post("/ai/drafts/", {"kind": "email_edit", "instruction": "shorter", "parts": json.dumps(bad)})
    assert r.status_code == 200
    stored = AIDraft.objects.get(pk=r.json()["draft"]["id"])
    assert "{%" not in stored.context["parts"]["heading"]
    assert owner.post("/ai/drafts/", {"kind": "email_edit", "instruction": "shorter", "parts": "nope"}).status_code == 400
    assert owner.post("/ai/drafts/", {"kind": "email_template", "prompt": ""}).status_code == 400


@pytest.mark.django_db
def test_no_ai_card_when_ai_is_off_and_the_form_still_works(owner, account):
    assert "Describe the email you need" in owner.get("/email/templates/").content.decode()
    AISettings.objects.filter(account=account).update(enabled=False)
    page = owner.get("/email/templates/").content.decode()
    assert "Describe the email you need" not in page and "Create template" in page
    owner.post("/email/templates/create/", {"name": "Plain", "subject": "Hi"})
    assert EmailTemplate.objects.filter(account=account, name="Plain").exists()


@pytest.mark.django_db
def test_another_businesss_draft_is_ignored(owner, account):
    other = Account.objects.create(company_name="Other")
    theirs = AIDraft.objects.create(account=other, kind="email_template", prompt="x", status="ready",
                                    result={"fields": {"name": "Secret", "subject": "s", "text_body": "t",
                                                       "html_body": "h", "sample_variables": {}}})
    assert "Why I built it" not in owner.get(f"/email/templates/?draft={theirs.pk}").content.decode()


@pytest.mark.django_db
def test_the_editor_offers_subject_lines(owner, account):
    template = EmailTemplate.objects.filter(account=account).first()
    page = owner.get(f"/email/templates/{template.pk}/").content.decode()
    assert "Suggest subject lines" in page
