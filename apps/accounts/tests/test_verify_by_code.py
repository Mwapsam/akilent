"""Phase 4 of the signup plan: confirming the owner's email with a code, what an unconfirmed
email holds back (campaigns, nothing else), the optional welcome card, and asking for the
postal address where it's needed."""

import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core import mail
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.accounts import email_mfa
from apps.accounts.models import Account, EmailLoginCode, Membership
from apps.accounts.tokens import email_verification_token
from apps.billing.models import Plan

VERIFY_URL = "/signup/verify/"
PW = "Sup3r-secret-pw"


@pytest.fixture
def owner(db):
    Plan.objects.get_or_create(
        slug=Plan.TRIAL,
        defaults={"name": "Trial", "price_monthly": 0, "trial_days": 14},
    )
    user = User.objects.create_user("new@example.com", "new@example.com", PW)
    account = Account.objects.create(company_name="New Co", billing_email=user.email)
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user, account


@pytest.fixture
def signed_in(client, owner):
    client.force_login(owner[0])
    return client


def _latest_code() -> str:
    return re.search(r"Your code is (\d{6})", mail.outbox[-1].body).group(1)


def _send(user):
    assert email_mfa.send_code(user, email_mfa.VERIFY) == email_mfa.SENT
    return _latest_code()


# ---- the code page --------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_right_code_confirms_the_address(signed_in, owner):
    from apps.seo.analytics import SESSION_KEY

    user, account = owner
    code = _send(user)
    resp = signed_in.post(VERIFY_URL, {"action": "verify", "code": code})
    assert resp.status_code == 302
    account.refresh_from_db()
    assert account.email_verified and account.email_verified_at
    assert signed_in.session[SESSION_KEY]["name"] == "email_verified"
    # Once confirmed, the page just moves on.
    assert signed_in.get(VERIFY_URL).status_code == 302


@pytest.mark.django_db
def test_a_wrong_code_says_so_and_confirms_nothing(signed_in, owner):
    user, account = owner
    _send(user)
    resp = signed_in.post(VERIFY_URL, {"action": "verify", "code": "000000"})
    assert resp.status_code == 400
    assert b"isn&#x27;t right" in resp.content
    account.refresh_from_db()
    assert not account.email_verified


@pytest.mark.django_db
def test_the_code_email_carries_the_code_and_the_link(owner):
    user, _ = owner
    _send(user)
    message = mail.outbox[-1]
    assert message.subject == "Confirm your email address"
    assert "/verify/" in message.body and re.search(r"\d{6}", message.body)


@pytest.mark.django_db
def test_sign_in_codes_keep_their_own_email(owner):
    user, _ = owner
    email_mfa.send_code(user, EmailLoginCode.Purpose.LOGIN)
    assert mail.outbox[-1].subject == email_mfa.CODE_SUBJECT


@pytest.mark.django_db
def test_skip_goes_on_to_setup_without_confirming(signed_in, owner):
    from apps.seo.analytics import SESSION_KEY

    _, account = owner
    resp = signed_in.post(VERIFY_URL, {"action": "skip"})
    assert resp.status_code == 302 and resp.url != VERIFY_URL
    account.refresh_from_db()
    assert not account.email_verified
    assert signed_in.session[SESSION_KEY]["name"] == "email_verify_skipped"


@pytest.mark.django_db
def test_resend_inside_the_cooldown_sends_nothing(signed_in, owner):
    from django.contrib.messages import get_messages

    user, _ = owner
    _send(user)
    sent = len(mail.outbox)
    resp = signed_in.post(VERIFY_URL, {"action": "resend"})
    assert len(mail.outbox) == sent
    assert any("moments ago" in str(m) for m in get_messages(resp.wsgi_request))


@pytest.mark.django_db
def test_the_link_in_the_same_email_still_works(client, owner):
    user, account = owner
    _send(user)
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = email_verification_token.make_token(user)
    client.get(f"/verify/{uid}/{token}/")
    account.refresh_from_db()
    assert account.email_verified


@pytest.mark.django_db
def test_a_dead_link_offers_a_new_code_when_signed_in(signed_in, owner):
    user, _ = owner
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    resp = signed_in.get(f"/verify/{uid}/not-a-token/")
    assert resp.status_code == 400
    assert b"Send a new code" in resp.content


# ---- limits ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_eleventh_guess_in_an_hour_fails_even_when_right(owner):
    user, _ = owner
    for _ in range(2):
        _send(user)
        EmailLoginCode.objects.update(created_at=timezone.now() - timedelta(minutes=2))
    for row in EmailLoginCode.objects.filter(purpose=email_mfa.VERIFY):
        row.attempts = 5
        row.save(update_fields=["attempts"])
    code = _send(user)
    assert not email_mfa.check_code(user, email_mfa.VERIFY, code)
    assert email_mfa.guess_limit_reached(user, email_mfa.VERIFY)


@pytest.mark.django_db
def test_five_wrong_guesses_kill_a_code(owner):
    user, _ = owner
    code = _send(user)
    for _ in range(5):
        assert not email_mfa.check_code(user, email_mfa.VERIFY, "000000")
    assert not email_mfa.check_code(user, email_mfa.VERIFY, code)


@pytest.mark.django_db
def test_verify_guesses_never_use_up_the_sign_in_allowance(owner):
    user, _ = owner
    _send(user)
    EmailLoginCode.objects.update(attempts=10)  # the verify pool is spent
    assert email_mfa.guess_limit_reached(user, email_mfa.VERIFY)
    assert not email_mfa.guess_limit_reached(user, EmailLoginCode.Purpose.LOGIN)
    email_mfa.send_code(user, EmailLoginCode.Purpose.LOGIN)
    assert email_mfa.check_code(user, EmailLoginCode.Purpose.LOGIN, _latest_code())


# ---- a different email ----------------------------------------------------------------------


@pytest.mark.django_db
def test_changing_the_email_moves_everything_and_voids_the_old_code(signed_in, owner):
    user, account = owner
    old_code = _send(user)
    old_token = email_verification_token.make_token(user)
    EmailLoginCode.objects.update(created_at=timezone.now() - timedelta(minutes=2))

    resp = signed_in.post(
        VERIFY_URL, {"action": "change_email", "email": "right@example.com"}
    )
    assert resp.status_code == 302
    user.refresh_from_db()
    account.refresh_from_db()
    assert user.email == user.username == "right@example.com"
    assert account.billing_email == "right@example.com"
    assert mail.outbox[-1].to == ["right@example.com"]
    assert not email_mfa.check_code(user, email_mfa.VERIFY, old_code)
    assert not email_verification_token.check_token(user, old_token)


@pytest.mark.django_db
def test_changing_to_a_taken_email_is_refused(signed_in, owner):
    User.objects.create_user("taken@example.com", "taken@example.com", PW)
    resp = signed_in.post(
        VERIFY_URL, {"action": "change_email", "email": "Taken@Example.com"}
    )
    assert resp.status_code == 400
    assert b"already exists" in resp.content
    owner[0].refresh_from_db()
    assert owner[0].email == "new@example.com"


# ---- what an unconfirmed email holds back ---------------------------------------------------


@pytest.mark.django_db
def test_email_campaigns_wait_for_a_confirmed_owner(owner):
    from apps.email.exceptions import UnconfirmedOwnerEmailError
    from apps.email.models import EmailDomain
    from apps.email.services.bulk import create_campaign

    _, account = owner
    Account.objects.filter(pk=account.pk).update(
        address_line1="1 Market St", city="Lusaka", country="ZM"
    )
    account.refresh_from_db()
    EmailDomain.objects.create(
        account=account, domain="newco.com", status=EmailDomain.Status.VERIFIED
    )
    with pytest.raises(UnconfirmedOwnerEmailError):
        create_campaign(
            account=account,
            from_email="hi@newco.com",
            recipients=[{"to": "a@example.com"}],
            text_override="Hi",
            subject_override="Hi",
        )


@pytest.mark.django_db
def test_whatsapp_campaigns_wait_for_a_confirmed_owner(owner):
    from apps.whatsapp.campaigns import CampaignError, _check_owner_confirmed

    _, account = owner
    with pytest.raises(CampaignError, match="Confirm your email"):
        _check_owner_confirmed(account)
    account.email_verified = True
    _check_owner_confirmed(account)  # no error once confirmed


@pytest.mark.django_db
def test_accounts_from_before_the_rule_are_marked_confirmed(owner):
    import importlib

    from django.apps import apps as django_apps

    _, account = owner
    assert not account.email_verified
    migration = importlib.import_module(
        "apps.accounts.migrations.0029_verify_existing_accounts"
    )
    migration.verify_existing(django_apps, None)
    account.refresh_from_db()
    assert account.email_verified and account.email_verified_at


@pytest.mark.django_db
def test_the_composer_says_what_is_missing_before_a_campaign(signed_in, owner):
    from apps.billing import api as billing_api
    from apps.billing.features import LEGACY_FLAGS
    from apps.billing.models import PlanFeature

    _, account = owner
    PlanFeature.objects.get_or_create(
        plan=account.subscription.plan, key=LEGACY_FLAGS["bulk_email"]
    )
    billing_api.forget()
    body = signed_in.get("/email/campaigns/new/").content.decode()
    assert "Confirm your email address to send campaigns." in body
    assert "/settings/business/?next=" in body  # comes back here once saved


# ---- address at the point of need -----------------------------------------------------------


@pytest.mark.django_db
def test_saving_the_address_goes_back_where_it_was_asked_for(signed_in, owner):
    data = {
        "address_line1": "1 Market St",
        "city": "Lusaka",
        "country": "Zambia",
        "next": "/email/campaigns/new/",
    }
    resp = signed_in.post("/settings/business/", data)
    assert resp.status_code == 302 and resp.url == "/email/campaigns/new/"
    data["next"] = "https://evil.example.com/"
    resp = signed_in.post("/settings/business/", data)
    assert resp.url == "/settings/business/"


# ---- the welcome card -----------------------------------------------------------------------


@pytest.mark.django_db
def test_welcome_card_asks_until_answered(signed_in, owner):
    _, account = owner
    Account.objects.filter(pk=account.pk).update(email_verified=True)
    assert b"Tell us about your business" in signed_in.get("/dashboard/").content
    signed_in.post(
        "/dashboard/welcome/",
        {"action": "save", "industry": "Real estate", "company_size": "2-5"},
    )
    account.refresh_from_db()
    assert (account.industry, account.company_size) == ("Real estate", "2-5")
    assert b"Tell us about your business" not in signed_in.get("/dashboard/").content


@pytest.mark.django_db
def test_welcome_card_keeps_an_old_free_text_answer(signed_in, owner):
    from apps.accounts.welcome import OTHER, WelcomeProfileForm

    _, account = owner
    Account.objects.filter(pk=account.pk).update(industry="Boat repairs")
    account.refresh_from_db()
    form = WelcomeProfileForm(
        {"industry": OTHER, "company_size": "Just me"}, account=account
    )
    assert form.initial["industry"] == OTHER and form.is_valid()
    form.save()
    account.refresh_from_db()
    assert account.industry == "Boat repairs"
    assert account.company_size == "Just me"


@pytest.mark.django_db
def test_no_thanks_hides_the_welcome_card(signed_in, owner):
    signed_in.post("/dashboard/welcome/", {"action": "dismiss"})
    assert b"Tell us about your business" not in signed_in.get("/dashboard/").content
