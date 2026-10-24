"""A rejected form (CSRF) is explained and, where safe, recovered — and the reason logged."""

import re

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan

PW = "Sup3r-secret-pw"


def _signup_data(token):
    return {
        "csrfmiddlewaretoken": token,
        "email": "a@example.com",
        "first_name": "A",
        "last_name": "B",
        "phone": "+260900000000",
        "company_name": "Co",
        "address_line1": "1 Main St",
        "city": "Lusaka",
        "country": "Zambia",
        "selected_services": "both",
        "plan": Plan.TRIAL,
        "password1": PW,
        "password2": PW,
    }


@pytest.fixture
def trial(db, settings):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    return Plan.objects.create(
        slug=Plan.TRIAL,
        name="Trial",
        price_monthly=0,
        trial_days=14,
        service_type=Plan.SERVICE_BOTH,
    )


def _rotate(client):
    """What signing in does to the CSRF cookie (force_login doesn't)."""
    from django.utils.crypto import get_random_string

    client.cookies["csrftoken"] = get_random_string(32)


def _token(resp):
    return re.search(
        r'name="csrfmiddlewaretoken" value="([^"]+)"', resp.content.decode()
    ).group(1)


@pytest.mark.django_db
def test_signup_page_is_never_cached(client, trial):
    resp = client.get("/signup/")
    assert "no-store" in resp["Cache-Control"]


@pytest.mark.django_db
def test_resubmitting_signup_after_signing_up_goes_on_to_setup(trial, caplog):
    """Back button after a successful signup: the old form's token predates the login."""
    c = Client(enforce_csrf_checks=True)
    data = _signup_data(_token(c.get("/signup/")))
    assert c.post("/signup/", data).status_code == 302

    with caplog.at_level("WARNING", logger="apps.core.views"):
        resp = c.post("/signup/", data)
    # Where signup itself sends them: their first setup step.
    assert resp.status_code == 302 and resp.url == "/whatsapp/numbers/"
    assert User.objects.filter(email="a@example.com").count() == 1
    assert any("csrf: rejected POST /signup/" in r.getMessage() for r in caplog.records)


@pytest.mark.django_db
def test_anonymous_rejection_explains_and_links_back(trial):
    c = Client(enforce_csrf_checks=True)
    c.get("/signup/")
    resp = c.post("/signup/", _signup_data("stale-token-from-an-old-page"))
    assert resp.status_code == 403
    assert b"This page expired" in resp.content
    assert b'href="/signup/"' in resp.content
    assert not User.objects.exists()


@pytest.mark.django_db
def test_fresh_token_endpoint_lets_a_restored_page_submit(trial):
    c = Client(enforce_csrf_checks=True)
    old = _token(c.get("/signup/"))
    # Something rotates the token after the page was rendered (signing in elsewhere).
    user = User.objects.create_user("x", "x@example.com", PW)
    acc = Account.objects.create(company_name="X")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    c.force_login(user)
    c.logout()
    c.get("/")  # a new anonymous session, as after signing out
    _rotate(c)

    # The page's own token is now stale and is refused...
    assert c.post("/signup/", _signup_data(old)).status_code == 403
    assert not User.objects.filter(email="a@example.com").exists()

    # ...and the one app.js fetches on restore works.
    fresh = c.get("/internal/csrf-token/")
    assert fresh.status_code == 200 and "no-store" in fresh["Cache-Control"]
    assert c.post("/signup/", _signup_data(fresh.json()["token"])).status_code == 302


@pytest.mark.django_db
def test_resent_paid_signup_resumes_at_checkout(trial):
    Plan.objects.create(
        slug=Plan.STARTER, name="Starter", price_monthly=19, service_type="email"
    )
    c = Client(enforce_csrf_checks=True)
    data = _signup_data(_token(c.get("/signup/")))
    data.update(selected_services="email", plan=Plan.STARTER)
    first = c.post("/signup/", data)
    assert first.url.startswith("/billing/checkout/")

    again = c.post("/signup/", data)
    assert again.status_code == 302 and again.url == first.url


@pytest.mark.django_db
def test_signed_in_as_someone_else_is_explained_not_redirected(trial):
    c = Client(enforce_csrf_checks=True)
    token = _token(c.get("/signup/"))
    other = User.objects.create_user("o", "other@example.com", PW)
    acc = Account.objects.create(company_name="Other")
    Membership.objects.create(user=other, account=acc, role=Membership.Role.OWNER)
    c.force_login(other)  # "another tab"
    _rotate(c)

    resp = c.post("/signup/", _signup_data(token))
    assert resp.status_code == 403
    assert b"other@example.com" in resp.content
    assert not User.objects.filter(email="a@example.com").exists()


@pytest.mark.django_db
def test_retry_link_is_the_page_the_form_was_on(trial):
    c = Client(enforce_csrf_checks=True)
    c.get("/signup/")
    resp = c.post(
        "/signup/",
        _signup_data("stale"),
        HTTP_REFERER="http://testserver/signup/?plan=trial",
    )
    assert b'href="http://testserver/signup/?plan=trial"' in resp.content


@pytest.mark.django_db
def test_retry_link_never_points_at_a_post_only_url_or_another_site(trial):
    c = Client(enforce_csrf_checks=True)
    c.get("/signup/")
    resp = c.post(
        "/email/domains/1/delete/",
        {"x": "1"},
        HTTP_REFERER="https://evil.example/phish",
    )
    assert resp.status_code == 403
    assert b"evil.example" not in resp.content
    assert b"Open the page again" not in resp.content


@pytest.mark.django_db
def test_htmx_gets_a_marked_bare_403(trial):
    c = Client(enforce_csrf_checks=True)
    c.get("/signup/")
    resp = c.post("/signup/", _signup_data("stale"), HTTP_HX_REQUEST="true")
    assert resp.status_code == 403
    assert resp["X-CSRF-Failed"] == "1"
    assert resp.content == b""


def test_failure_view_survives_a_bare_request(rf):
    from apps.core.views import csrf_failure

    request = rf.post("/x/", HTTP_HOST="not-allowed.example")
    request._messages = None  # no middleware ran
    resp = csrf_failure(request, reason="CSRF cookie not set.")
    assert resp.status_code == 403
