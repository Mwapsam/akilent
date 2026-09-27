"""The public landing page: owner-first copy that only claims what the product does."""
import pytest

from apps.billing.models import Plan
from apps.core.models import SiteSettings

# Claims the page used to make that nothing in the product backs up.
UNBACKED = ("99.9%", "Enterprise", "Usage-based", "infrastructure layer")


@pytest.fixture
def plans(db):
    return [
        Plan.objects.create(slug=Plan.TRIAL, name="Trial", price_monthly=0, trial_days=14),
        Plan.objects.create(slug=Plan.STARTER, name="Starter", price_monthly=19),
        Plan.objects.create(slug=Plan.PROFESSIONAL, name="Professional", price_monthly=49),
    ]


def _get(client):
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("whatsapp", [True, False])
def test_renders_without_unbacked_claims(client, settings, plans, whatsapp):
    settings.WHATSAPP_ENABLED = whatsapp
    html = _get(client)
    for phrase in UNBACKED:
        assert phrase not in html, phrase
    for plan in plans:
        assert plan.name in html


@pytest.mark.django_db
def test_whatsapp_hero_only_when_whatsapp_is_on(client, settings, plans):
    settings.WHATSAPP_ENABLED = True
    assert "Answer every customer on WhatsApp" in _get(client)

    settings.WHATSAPP_ENABLED = False
    html = _get(client)
    assert "Answer every customer on WhatsApp" not in html
    assert "mock-frame\" aria-hidden" not in html
    hero = html.split('<section class="hero dark">', 1)[1].split("</section>", 1)[0]
    assert "WhatsApp" not in hero


@pytest.mark.django_db
def test_trial_line_follows_the_plan(client, plans):
    assert "14-day free trial" in _get(client)

    Plan.objects.filter(slug=Plan.TRIAL).update(trial_days=7)
    assert "7-day free trial" in _get(client)


# The hero links to a plain /signup/, so its number must be what auto_create_trial grants.

@pytest.mark.django_db
def test_trial_line_falls_back_to_site_default_like_signup(client, plans):
    Plan.objects.filter(slug=Plan.TRIAL).update(trial_days=0)
    site = SiteSettings.load()
    site.default_trial_days = 14
    site.save()
    assert "14-day free trial" in _get(client)


@pytest.mark.django_db
def test_paid_plan_trial_is_not_advertised(client, plans):
    """Paid plans never get a trial at signup, so their trial_days must not reach the hero."""
    Plan.objects.filter(slug=Plan.TRIAL).delete()
    Plan.objects.filter(slug=Plan.STARTER).update(trial_days=7)
    assert "-day free trial" not in _get(client)


@pytest.mark.django_db
def test_trial_line_uses_the_sites_default_plan(client, plans):
    starter = Plan.objects.get(slug=Plan.STARTER)
    starter.trial_days = 30
    starter.save()
    site = SiteSettings.load()
    site.default_plan = starter
    site.save()
    assert "30-day free trial" in _get(client)


@pytest.mark.django_db
def test_hero_trial_matches_the_subscription_signup_creates(plans):
    from apps.accounts.models import Account
    from apps.billing import api as billing_api

    Plan.objects.filter(slug=Plan.TRIAL).update(trial_days=0)
    site = SiteSettings.load()
    site.default_trial_days = 10
    site.save()

    _, advertised = billing_api.default_signup_trial()
    account = Account.objects.create(company_name="Trial Co")
    sub = account.subscription
    assert advertised == 10
    assert (sub.trial_ends_at - sub.current_period_start).days == advertised


@pytest.mark.django_db
def test_free_plan_card_only_says_trial_when_the_plan_has_one(client, plans):
    Plan.objects.filter(slug=Plan.TRIAL).update(trial_days=0)
    html = _get(client)
    pricing = html.split('id="pricing"', 1)[1].split("</section>", 1)[0]
    assert "Get started free" in pricing
    assert "Start free trial" not in pricing


@pytest.mark.django_db
def test_footer_shows_business_contact_not_personal_details(client, plans):
    site = SiteSettings.load()
    site.support_email = "hello@akilent.test"
    site.save()

    html = _get(client)
    assert "mailto:hello@akilent.test" in html
    assert "Lusaka, Zambia" in html
    assert "tel:" not in html
    assert "Kajema" not in html


@pytest.mark.django_db
def test_header_follows_sign_in_state(client, plans, django_user_model):
    html = _get(client)
    assert "Sign in" in html and "Open dashboard" not in html

    client.force_login(django_user_model.objects.create_user(
        username="owner@example.com", email="owner@example.com", password="x"))
    html = _get(client)
    assert "Open dashboard" in html and 'href="/auth/login/"' not in html


@pytest.mark.django_db
def test_mobile_menu_is_a_real_button(client, plans):
    """A checkbox hack failed Lighthouse (unlabelled input, ARIA on a <label>)."""
    html = _get(client)
    assert 'id="menu-button"' in html and 'aria-expanded="false"' in html
    assert 'type="checkbox"' not in html


@pytest.mark.django_db
def test_api_example_is_a_valid_request(client, plans):
    html = _get(client)
    # MessageCreateSerializer requires from_email and to_email.
    assert '"from_email"' in html and '"to_email"' in html
