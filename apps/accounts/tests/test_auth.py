import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts.models import Account, Membership
from apps.accounts.tokens import email_verification_token
from apps.billing import api as billing_api
from apps.billing.models import Plan, Subscription

SIGNUP_URL = "/signup/"

PW = "Sup3r-secret-pw"


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
        "selected_channels": ["email"],
        "plan": Plan.STARTER,
        "password1": PW,
        "password2": PW,
    }
    data.update(overrides)
    return data


@pytest.fixture
def trial_plan(db):
    # Present so the auto_create_trial signal attaches a trial subscription.
    return Plan.objects.create(
        slug=Plan.TRIAL,
        name="Trial",
        price_monthly=0,
        trial_days=14,
    )


@pytest.fixture
def email_plan(db):
    # $0: these fixtures exercise onboarding routing/login/verification, not
    # billing — kept free so signup completes without a Stripe checkout
    # redirect. See test_signup_billing.py for paid-plan behavior.
    plan = Plan.objects.create(slug=Plan.STARTER, name="Starter", price_monthly=0)
    billing_api.set_plan_channels(plan, ["email"])
    return plan


@pytest.fixture
def whatsapp_plan(db):
    plan = Plan.objects.create(
        slug=Plan.PROFESSIONAL, name="Professional", price_monthly=0
    )
    billing_api.set_plan_channels(plan, ["whatsapp"])
    return plan


@pytest.mark.django_db
def test_signup_creates_active_account_and_sends_verification(
    client, trial_plan, email_plan
):
    resp = client.post(SIGNUP_URL, _payload())
    assert resp.status_code == 302
    assert resp.url == "/email/domains/"  # email-only → straight to domain setup

    user = User.objects.get(email="new@example.com")
    assert user.is_active is True
    assert user.username == "new@example.com"
    assert (user.first_name, user.last_name) == ("Ada", "Lovelace")

    account = Account.objects.get(company_name="New Co")
    assert account.selected_channels == ["email"]
    assert account.onboarding_state == Account.Onboarding.DOMAIN_SETUP
    assert account.email_verified is False
    # The address isn't asked for at signup (it's asked for before the first email
    # campaign), so whatever is posted for it is ignored.
    assert account.address_line1 == "" and account.country == "" and account.phone == ""
    assert Membership.objects.filter(
        user=user, account=account, role=Membership.Role.OWNER
    ).exists()

    subscription = Subscription.objects.get(account=account)
    assert subscription.plan_id == email_plan.pk

    assert len(mail.outbox) == 1
    assert "new@example.com" in mail.outbox[0].to

    # Logged in immediately.
    assert client.session.get("_auth_user_id") == str(user.pk)


@pytest.mark.django_db
def test_signup_whatsapp_routes_to_meta_onboarding(
    client, settings, trial_plan, whatsapp_plan
):
    settings.WHATSAPP_ENABLED = True
    resp = client.post(
        SIGNUP_URL,
        _payload(
            email="wa@example.com",
            company_name="WA Co",
            selected_channels=["whatsapp"],
            plan=Plan.PROFESSIONAL,
        ),
    )
    assert resp.status_code == 302
    assert resp.url == "/whatsapp/numbers/"
    account = Account.objects.get(company_name="WA Co")
    assert account.onboarding_state == Account.Onboarding.WHATSAPP_SETUP


@pytest.mark.django_db
def test_signup_rejects_plan_service_mismatch(
    client, trial_plan, email_plan, whatsapp_plan
):
    resp = client.post(
        SIGNUP_URL,
        _payload(selected_channels=["whatsapp"], plan=Plan.STARTER),
    )
    assert resp.status_code == 400
    assert not Account.objects.filter(company_name="New Co").exists()
    assert not User.objects.filter(email="new@example.com").exists()


@pytest.mark.django_db
def test_active_user_can_log_in_with_email(client, trial_plan, email_plan):
    client.post(
        SIGNUP_URL, _payload(email="active@example.com", company_name="Active Co")
    )
    client.logout()
    assert client.login(username="active@example.com", password=PW) is True


@pytest.mark.django_db
def test_login_view_redirects_to_dashboard(client, trial_plan, email_plan):
    client.post(
        SIGNUP_URL, _payload(email="viaform@example.com", company_name="Via Co")
    )
    client.logout()
    resp = client.post(
        reverse("login"), {"username": "viaform@example.com", "password": PW}
    )
    assert resp.status_code == 302
    assert resp.url == "/dashboard/"


@pytest.mark.django_db
def test_verify_email_marks_account_verified(client, trial_plan, email_plan):
    client.post(
        SIGNUP_URL, _payload(email="confirm@example.com", company_name="Confirm Co")
    )
    user = User.objects.get(email="confirm@example.com")
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = email_verification_token.make_token(user)

    resp = client.get(reverse("verify_email", kwargs={"uidb64": uid, "token": token}))
    assert resp.status_code == 302
    account = Account.objects.get(company_name="Confirm Co")
    assert account.email_verified is True
    assert account.email_verified_at is not None


@pytest.mark.django_db
def test_verify_email_survives_a_re_login(client, trial_plan, email_plan):
    """The link must still validate after the owner signs in again."""
    client.post(
        SIGNUP_URL, _payload(email="relog@example.com", company_name="Relog Co")
    )
    user = User.objects.get(email="relog@example.com")
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = email_verification_token.make_token(user)

    client.logout()
    client.login(username="relog@example.com", password=PW)  # bumps last_login

    resp = client.get(reverse("verify_email", kwargs={"uidb64": uid, "token": token}))
    assert resp.status_code == 302
    assert Account.objects.get(company_name="Relog Co").email_verified is True


@pytest.mark.django_db
def test_verify_email_rejects_bad_token(client, trial_plan, email_plan):
    client.post(SIGNUP_URL, _payload(email="bad@example.com", company_name="Bad Co"))
    user = User.objects.get(email="bad@example.com")
    uid = urlsafe_base64_encode(force_bytes(user.pk))

    resp = client.get(
        reverse("verify_email", kwargs={"uidb64": uid, "token": "wrong-token"})
    )
    assert resp.status_code == 400
    assert Account.objects.get(company_name="Bad Co").email_verified is False


@pytest.fixture
def paid_whatsapp_plan(db):
    plan = Plan.objects.create(
        slug=Plan.PROFESSIONAL, name="Professional", price_monthly=49
    )
    billing_api.set_plan_channels(plan, ["whatsapp"])
    return plan


def _preselect(resp):
    assert resp.status_code == 200
    return resp.context["wizard_config"]["preselect"]


@pytest.mark.django_db
def test_signup_honors_paid_plan_query_param(client, trial_plan, paid_whatsapp_plan):
    pre = _preselect(client.get(SIGNUP_URL + "?plan=professional"))
    assert pre["plan"] == "professional"
    assert pre["channels"] == ["whatsapp"]
    assert pre["startStep"] == "account"
    assert pre["entry"] == "plans"
    assert pre["steps"] == ["channels", "package", "account", "review"]


@pytest.mark.django_db
def test_a_free_plan_link_skips_the_package_step(client, trial_plan, whatsapp_plan):
    pre = _preselect(client.get(SIGNUP_URL + "?plan=professional"))
    assert pre["plan"] == "professional"
    # The customer picks from what the plan gives, then goes on to their details.
    assert pre["channels"] == [] and pre["startStep"] == "channels"
    assert pre["entry"] == "trial"
    assert pre["steps"] == ["channels", "account", "review"]


@pytest.mark.django_db
def test_plain_signup_starts_on_the_default_trial(client, trial_plan, email_plan):
    pre = _preselect(client.get(SIGNUP_URL))
    assert pre["plan"] == Plan.TRIAL
    assert pre["entry"] == "trial"
    assert pre["startStep"] == "channels"
    assert pre["fromQuery"] is False


@pytest.mark.django_db
def test_plain_signup_without_a_free_trial_shows_every_package(client, email_plan):
    pre = _preselect(client.get(SIGNUP_URL))
    assert pre["entry"] == "plans" and pre["plan"] == ""


@pytest.mark.django_db
def test_step_in_the_url_never_skips_what_is_missing(client, trial_plan, email_plan):
    # A shared link with nothing picked opens on Channels.
    pre = _preselect(client.get(SIGNUP_URL + "?step=account"))
    assert pre["startStep"] == "channels"
    # With the channels and plan in the link, an earlier step can be asked for...
    pre = _preselect(client.get(SIGNUP_URL + "?plan=trial&channels=email&step=channels"))
    assert pre["startStep"] == "channels"
    # ...and the furthest one the link reaches is the default.
    pre = _preselect(client.get(SIGNUP_URL + "?plan=trial&channels=email"))
    assert pre["startStep"] == "account" and pre["channels"] == ["email"]
    assert pre["fromQuery"] is True


@pytest.mark.django_db
def test_a_bounced_trial_signup_keeps_its_entry(client, trial_plan, email_plan):
    resp = client.post(
        SIGNUP_URL, _payload(plan=Plan.TRIAL, entry="trial", password2="different")
    )
    assert resp.status_code == 400
    cfg = resp.context["wizard_config"]
    assert cfg["preselect"]["entry"] == "trial"
    assert "password2" in cfg["errors"]
    # Passwords are never sent back to the page.
    assert "password1" not in cfg["formData"]


@pytest.mark.django_db
def test_signup_queues_a_signup_created_event(client, trial_plan, email_plan):
    from apps.seo.analytics import SESSION_KEY

    client.post(SIGNUP_URL, _payload())
    assert client.session[SESSION_KEY] == {
        "name": "signup_created",
        "params": {"plan": Plan.STARTER},
    }


@pytest.mark.django_db
def test_signup_ignores_unknown_plan_slug(client, trial_plan):
    resp = client.get(SIGNUP_URL + "?plan=not-a-real-plan")
    assert resp.status_code == 200
    assert resp.context["wizard_config"]["preselect"]["startStep"] == "channels"


@pytest.mark.django_db
def test_signup_disabled_redirects_to_landing_pricing(client, trial_plan):
    from apps.core.models import SiteSettings

    site = SiteSettings.load()
    site.signups_enabled = False
    site.save(update_fields=["signups_enabled"])

    resp = client.get(SIGNUP_URL)
    assert resp.status_code == 302
    assert resp.url == "/#pricing"


@pytest.mark.django_db
def test_resend_verification_for_signed_in_owner(client, trial_plan, email_plan):
    client.post(
        SIGNUP_URL, _payload(email="resend@example.com", company_name="Resend Co")
    )
    assert len(mail.outbox) == 1

    resp = client.post(reverse("resend-verification"))
    assert resp.status_code == 302
    assert len(mail.outbox) == 2
    assert "resend@example.com" in mail.outbox[1].to


@pytest.mark.django_db
def test_resend_verification_unknown_email_shows_same_page(client, trial_plan):
    resp = client.post(reverse("resend-verification"), {"email": "nobody@example.com"})
    assert resp.status_code == 200
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_signup_needs_no_address_or_phone(client, trial_plan, email_plan):
    data = _payload()
    for key in ("phone", "address_line1", "city", "country"):
        data.pop(key)
    resp = client.post(SIGNUP_URL, data)
    assert resp.status_code == 302, resp.content[:2000]
    assert Account.objects.filter(company_name="New Co").exists()
