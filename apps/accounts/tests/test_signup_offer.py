"""Signup only offers what can be sold right now, whichever channels are switched off."""

import json

import pytest
from django.contrib.auth.models import User

from apps.accounts.forms import effective_service, signup_offer
from apps.accounts.models import Account
from apps.billing.models import Plan
from apps.core import channels as ch

SIGNUP_URL = "/signup/"
PW = "Sup3r-secret-pw"


@pytest.fixture(autouse=True)
def _fresh_channel_cache():
    reset = {"value": None, "loaded_at": 0.0, "failed_at": 0.0}
    ch._cache.update(reset)
    yield
    ch._cache.update(reset)


@pytest.fixture
def plans(db):
    trial = Plan.objects.create(
        slug=Plan.TRIAL,
        name="Trial",
        price_monthly=0,
        trial_days=14,
        service_type=Plan.SERVICE_BOTH,
    )
    starter = Plan.objects.create(
        slug=Plan.STARTER,
        name="Starter",
        price_monthly=19,
        service_type=Plan.SERVICE_EMAIL,
    )
    return trial, starter


def _payload(**overrides):
    data = {
        "email": "new@example.com",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "phone": "+260 900 000 000",
        "company_name": "New Co",
        "address_line1": "1 Main St",
        "city": "Lusaka",
        "country": "Zambia",
        "selected_services": Account.Services.WHATSAPP,
        "plan": Plan.TRIAL,
        "password1": PW,
        "password2": PW,
    }
    data.update(overrides)
    return data


def _wizard_config(resp):
    html = resp.content.decode()
    start = html.index('id="signup-wizard-config"')
    body = html[html.index(">", start) + 1 : html.index("</script>", start)]
    return json.loads(body)


def test_effective_service():
    assert effective_service("both", {"email", "whatsapp"}) == "both"
    assert effective_service("both", {"whatsapp"}) == "whatsapp"
    assert effective_service("both", {"email"}) == "email"
    assert effective_service("email", {"whatsapp"}) is None
    assert effective_service("whatsapp", set()) is None


@pytest.mark.django_db
def test_both_plan_becomes_a_whatsapp_plan_when_email_is_off(plans, settings):
    settings.EMAIL_ENABLED = False
    settings.WHATSAPP_ENABLED = True
    by_service, choices = signup_offer()
    assert [v for v, _ in choices] == ["whatsapp"]
    assert [p.slug for p in by_service["whatsapp"]] == [Plan.TRIAL]


@pytest.mark.django_db
def test_service_without_a_plan_is_not_offered(db, settings):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    Plan.objects.create(slug="e", name="E", service_type=Plan.SERVICE_EMAIL)
    _, choices = signup_offer()
    assert [v for v, _ in choices] == ["email"]


@pytest.mark.django_db
def test_wizard_offers_trial_for_whatsapp_when_email_is_off(client, plans, settings):
    settings.EMAIL_ENABLED = False
    settings.WHATSAPP_ENABLED = True
    cfg = _wizard_config(client.get(SIGNUP_URL))
    assert [c["value"] for c in cfg["serviceChoices"]] == ["whatsapp"]
    assert [(p["slug"], p["serviceType"]) for p in cfg["plans"]] == [
        (Plan.TRIAL, "whatsapp")
    ]


@pytest.mark.django_db
def test_signup_with_email_off_succeeds_on_the_trial(client, plans, settings):
    settings.EMAIL_ENABLED = False
    settings.WHATSAPP_ENABLED = True
    resp = client.post(SIGNUP_URL, _payload())
    assert resp.status_code == 302, resp.content[:2000]
    # Straight to the channel that's on, not to a checklist that's on hold.
    assert resp.url == "/whatsapp/numbers/"
    account = Account.objects.get(company_name="New Co")
    # Stored as what was bought, so email setup is there when email comes back.
    assert account.selected_services == Account.Services.BOTH
    assert account.subscription.plan.slug == Plan.TRIAL


@pytest.mark.django_db
def test_a_plan_for_a_switched_off_channel_is_rejected(client, plans, settings):
    settings.EMAIL_ENABLED = False
    settings.WHATSAPP_ENABLED = True
    resp = client.post(SIGNUP_URL, _payload(plan=Plan.STARTER))
    assert resp.status_code == 400
    assert not User.objects.filter(email="new@example.com").exists()


@pytest.mark.django_db
def test_no_services_shows_a_clear_message(client, plans, settings):
    settings.EMAIL_ENABLED = False
    settings.WHATSAPP_ENABLED = False
    resp = client.get(SIGNUP_URL)
    assert resp.status_code == 200
    assert b'data-testid="signup-unavailable"' in resp.content


@pytest.mark.django_db
def test_signup_survives_a_broker_outage(client, plans, settings, monkeypatch):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True

    def boom(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr("apps.accounts.tasks.send_verification_email.delay", boom)
    resp = client.post(SIGNUP_URL, _payload(selected_services=Account.Services.BOTH))
    assert resp.status_code == 302
    assert User.objects.filter(email="new@example.com").exists()


@pytest.mark.django_db
def test_landing_hides_plans_signup_cannot_sell(client, plans, settings):
    settings.EMAIL_ENABLED = False
    settings.WHATSAPP_ENABLED = True
    html = client.get("/").content.decode()
    assert "?plan=" + Plan.TRIAL in html
    assert "?plan=" + Plan.STARTER not in html


@pytest.mark.django_db
def test_lost_duplicate_race_is_reported_as_email_taken(
    client, plans, settings, monkeypatch
):
    from apps.accounts.forms import SignupForm

    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    # Another request created the user between clean_email and the insert.
    User.objects.create_user("new@example.com", "new@example.com", PW)
    monkeypatch.setattr(
        SignupForm, "clean_email", lambda self: self.cleaned_data["email"]
    )
    resp = client.post(SIGNUP_URL, _payload(selected_services=Account.Services.BOTH))
    assert resp.status_code == 400
    assert b"An account with this email already exists." in resp.content


@pytest.mark.django_db
def test_other_integrity_errors_are_not_passed_off_as_email_taken(
    client, plans, settings, monkeypatch
):
    from django.db import IntegrityError

    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True

    def broken(account):
        raise IntegrityError("duplicate module row")

    monkeypatch.setattr("apps.accounts.views._set_default_modules", broken)
    with pytest.raises(IntegrityError):
        client.post(SIGNUP_URL, _payload(selected_services=Account.Services.BOTH))
    assert not User.objects.filter(email="new@example.com").exists()


@pytest.mark.django_db
def test_resend_says_so_when_the_email_could_not_be_queued(
    client, plans, settings, monkeypatch
):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    client.post(SIGNUP_URL, _payload(selected_services=Account.Services.BOTH))

    def boom(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr("apps.accounts.tasks.send_verification_email.delay", boom)
    from django.contrib.messages import get_messages

    resp = client.post("/resend-verification/")
    assert resp.status_code == 302
    texts = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("couldn't send the email" in t for t in texts), texts
    assert not any("Sent" in t for t in texts)


@pytest.mark.django_db
def test_paid_signup_warns_when_verification_could_not_be_queued(
    client, plans, settings, monkeypatch
):
    from django.contrib.messages import get_messages

    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True

    def boom(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr("apps.accounts.tasks.send_verification_email.delay", boom)
    resp = client.post(
        SIGNUP_URL,
        _payload(selected_services=Account.Services.EMAIL, plan=Plan.STARTER),
    )
    assert resp.status_code == 302 and resp.url.startswith("/billing/checkout/")
    texts = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("couldn't send the verification email" in t for t in texts), texts


@pytest.mark.django_db
def test_landing_and_wizard_describe_a_plan_the_same_way(client, db, settings):
    from apps.accounts.views import _plan_feature_bullets
    from apps.billing.models import PlanFeature

    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    plan = Plan.objects.create(
        slug=Plan.PROFESSIONAL,
        name="Professional",
        price_monthly=49,
        service_type=Plan.SERVICE_WHATSAPP,
    )
    PlanFeature.objects.get_or_create(plan=plan, key="email_campaigns")
    PlanFeature.objects.get_or_create(plan=plan, key="whatsapp_campaigns")
    card = client.get("/").context["cards"][0]
    assert card["limits"] + card["feature_names"] == _plan_feature_bullets(
        plan, "whatsapp"
    )


# --- Billing term -------------------------------------------------------------


@pytest.mark.django_db
def test_wizard_shows_what_checkout_charges_for_each_term(client, plans, settings):
    from apps.billing.pricing import price_for

    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    cfg = _wizard_config(client.get(SIGNUP_URL))
    assert [o["value"] for o in cfg["periods"]] == [
        "monthly",
        "quarterly",
        "annually",
        "biennial",
    ]
    assert next(o for o in cfg["periods"] if o["value"] == "annually")["savePct"] == 15
    starter = Plan.objects.get(slug=Plan.STARTER)
    card = next(p for p in cfg["plans"] if p["slug"] == Plan.STARTER)
    for period, amount in card["periodPrices"].items():
        assert amount == float(price_for(starter, period))
    assert cfg["preselect"]["period"] == "monthly"


@pytest.mark.django_db
def test_period_in_the_link_is_preselected(client, plans, settings):
    settings.EMAIL_ENABLED = True
    cfg = _wizard_config(client.get(SIGNUP_URL + "?plan=starter&period=annually"))
    assert cfg["preselect"]["period"] == "annually"
    cfg = _wizard_config(client.get(SIGNUP_URL + "?period=weekly"))
    assert cfg["preselect"]["period"] == "monthly"


@pytest.mark.django_db
def test_paid_signup_is_billed_for_the_chosen_term(client, plans, settings):
    from apps.billing.models import Subscription

    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    resp = client.post(
        SIGNUP_URL,
        _payload(
            selected_services=Account.Services.EMAIL,
            plan=Plan.STARTER,
            billing_period="annually",
        ),
    )
    assert resp.status_code == 302
    assert resp.url == "/billing/checkout/?plan=starter&period=annually"
    sub = Account.objects.get(company_name="New Co").subscription
    assert sub.status == Subscription.INCOMPLETE
    assert sub.billing_period == "annually"


@pytest.mark.django_db
def test_unknown_term_is_rejected(client, plans, settings):
    settings.EMAIL_ENABLED = True
    resp = client.post(
        SIGNUP_URL,
        _payload(
            selected_services=Account.Services.EMAIL,
            plan=Plan.STARTER,
            billing_period="weekly",
        ),
    )
    assert resp.status_code == 400
    assert not User.objects.filter(email="new@example.com").exists()
    # Shown on the Package step, where the term is chosen.
    assert b"Choose how often you&#x27;d like to be billed." in resp.content
    assert _wizard_config(resp)["errors"]["billing_period"]


@pytest.mark.django_db
def test_a_free_plan_ignores_whatever_term_was_sent(client, plans, settings):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    resp = client.post(
        SIGNUP_URL,
        _payload(selected_services=Account.Services.BOTH, billing_period="weekly"),
    )
    assert resp.status_code == 302
    assert Account.objects.get(company_name="New Co").subscription.billing_period == (
        "monthly"
    )


@pytest.mark.django_db
def test_a_partial_discount_override_keeps_the_other_terms(client, plans, settings):
    """A promo that sets one term must not break signup, and Billing -> Plans agrees."""
    import json
    from decimal import Decimal

    from django.utils import timezone

    from apps.billing.models import Subscription
    from apps.billing.pricing import price_for

    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    settings.BILLING_PERIOD_MULTIPLIERS = {"annually": Decimal("0.80")}

    resp = client.get(SIGNUP_URL)
    assert resp.status_code == 200
    cfg = _wizard_config(resp)
    save = {o["value"]: o["savePct"] for o in cfg["periods"]}
    assert save == {"monthly": 0, "quarterly": 5, "annually": 20, "biennial": 25}
    starter = Plan.objects.get(slug=Plan.STARTER)
    assert price_for(starter, "annually") == Decimal("182.40")  # 19 x 12 x 0.80

    user = User.objects.create_user("p", "p@example.com", PW)
    acc = Account.objects.create(company_name="P")
    from apps.accounts.models import Membership

    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    Subscription.objects.update_or_create(
        account=acc,
        defaults={
            "plan": starter,
            "status": Subscription.ACTIVE,
            "current_period_start": timezone.now(),
        },
    )
    client.force_login(user)
    plans_page = client.get("/billing/plans/")
    pricing = json.loads(plans_page.context["period_pricing_json"])
    assert pricing["annually"]["multiplier"] == 0.8
    assert pricing["quarterly"]["multiplier"] == 0.95


@pytest.mark.django_db
def test_paid_plan_needs_a_known_term(plans):
    from apps.accounts.views import _apply_selected_plan

    acc = Account.objects.create(company_name="T")
    starter = Plan.objects.get(slug=Plan.STARTER)
    with pytest.raises(ValueError):
        _apply_selected_plan(acc, starter, None)


@pytest.mark.django_db
def test_free_plan_keeps_no_term(client, plans, settings):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    client.post(
        SIGNUP_URL,
        _payload(selected_services=Account.Services.BOTH, billing_period="annually"),
    )
    sub = Account.objects.get(company_name="New Co").subscription
    assert sub.plan.slug == Plan.TRIAL and sub.billing_period == "monthly"


@pytest.mark.django_db
def test_wizard_prices_use_the_billing_currency(client, plans, settings):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    settings.BILLING_CURRENCY = "ZMW"
    cfg = _wizard_config(client.get(SIGNUP_URL))
    assert cfg["currency"] == {"symbol": "K", "zeroDecimal": False}


@pytest.mark.django_db
def test_paid_signup_explains_payment_before_creating_the_account(
    client, plans, settings
):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    html = client.get(SIGNUP_URL).content.decode()
    assert "Create your account and pay" in html
    # The dialog's button submits the signup form itself.
    form = html[html.index('action="/signup/"') : html.index("</form>")]
    assert 'type="submit" class="btn btn-primary" @click="confirmed = true"' in form
