from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailTemplate, EmailTemplateLocale
from apps.email.services.render import render_template


@pytest.fixture
def api_key(db):
    user = User.objects.create_user("o", "o@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"),
                               max_emails_per_month=100, email_apis=True, email_templates=True,
                               api_rate_per_min=0)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    k, raw = EmailApiKey.create_for_account(acc, name="k")
    k.scopes = ["messages:send", "templates:manage"]
    k.save(update_fields=["scopes"])
    return raw, acc


@pytest.mark.django_db
def test_render_picks_locale_with_per_field_fallback(api_key):
    _, acc = api_key
    t = EmailTemplate.objects.create(account=acc, name="T", slug="t",
                                     subject="Hello {{ name }}", text_body="base text",
                                     html_body="<p>base</p>")
    EmailTemplateLocale.objects.create(template=t, locale="fr", subject="Bonjour {{ name }}")

    subj, text, html = render_template(t, {"name": "Ada"}, locale="fr")
    assert subj == "Bonjour Ada"
    assert text == "base text"  # falls back — locale row left text blank

    # pt-BR falls back to pt, then to base when neither exists
    subj_pt, _, _ = render_template(t, {"name": "Ada"}, locale="pt-BR")
    assert subj_pt == "Hello Ada"


@pytest.mark.django_db
def test_render_reads_locale_from_contact_variable(api_key):
    _, acc = api_key
    t = EmailTemplate.objects.create(account=acc, name="T", slug="t", subject="Hi")
    EmailTemplateLocale.objects.create(template=t, locale="de", subject="Hallo")
    subj, _, _ = render_template(t, {"contact": {"locale": "de"}})
    assert subj == "Hallo"


@pytest.mark.django_db
def test_locale_crud_endpoints(client, api_key):
    key, acc = api_key
    EmailTemplate.objects.create(account=acc, name="T", slug="t", subject="Hi")

    put = client.put("/api/v1/templates/t/locales/fr",
                     data={"subject": "Bonjour"}, content_type="application/json",
                     HTTP_X_API_KEY=key)
    assert put.status_code == 200

    lst = client.get("/api/v1/templates/t/locales", HTTP_X_API_KEY=key)
    assert [r["locale"] for r in lst.json()["data"]] == ["fr"]

    preview = client.post("/api/v1/templates/t/preview",
                          data={"locale": "fr"}, content_type="application/json",
                          HTTP_X_API_KEY=key)
    assert preview.json()["subject"] == "Bonjour"

    d = client.delete("/api/v1/templates/t/locales/fr", HTTP_X_API_KEY=key)
    assert d.status_code == 204
    assert client.get("/api/v1/templates/t/locales", HTTP_X_API_KEY=key).json()["data"] == []
