"""Phase 5 of the signup plan: the page shows something before its scripts run, the wide-screen
side panel, the phone stepper, the 404 and 500 pages, addresses people guess (/login/,
/register/) and autofilled fields in the theme's colours."""

import re

import pytest
from django.conf import settings
from django.template import Context, Template

from apps.billing import api as billing_api
from apps.billing.models import Plan
from apps.core import channels as ch

SIGNUP_URL = "/signup/"
TEMPLATES = settings.BASE_DIR / "templates"


@pytest.fixture(autouse=True)
def _whatsapp_on(settings):
    settings.WHATSAPP_ENABLED = True
    settings.EMAIL_ENABLED = False
    reset = {"value": None, "loaded_at": 0.0, "failed_at": 0.0}
    ch._cache.update(reset)
    yield
    ch._cache.update(reset)


@pytest.fixture
def plans(db):
    trial = Plan.objects.create(
        slug=Plan.TRIAL, name="Trial", price_monthly=0, trial_days=14
    )
    billing_api.set_plan_channels(trial, ["whatsapp"])
    paid = Plan.objects.create(
        slug=Plan.PROFESSIONAL, name="Professional", price_monthly=49
    )
    billing_api.set_plan_channels(paid, ["whatsapp"])
    return trial, paid


def _step_tag(html: str, key: str) -> str:
    return re.search(rf"<div x-show=\"step === '{key}'\"[^>]*>", html).group(0)


# ---- nothing waits for the scripts to be seen -----------------------------------------------


@pytest.mark.django_db
def test_the_start_step_is_drawn_before_the_scripts_run(client, plans):
    html = client.get(SIGNUP_URL).content.decode()
    root = re.search(r'<div[^>]*x-data="signupWizard\(\)"[^>]*>', html).group(0)
    assert "x-cloak" not in root
    assert "x-cloak" not in _step_tag(html, "channels")
    for later in ("account", "review"):
        assert "x-cloak" in _step_tag(html, later)
    # The channel cards are in the HTML, not only in an Alpine template.
    assert re.search(r'<div x-show="false" class="choice-card[^"]*">', html)
    assert "start your free trial" in html  # the subtitle, without waiting for x-text


@pytest.mark.django_db
def test_a_link_that_starts_on_account_draws_that_step(client, plans):
    html = client.get(SIGNUP_URL + "?plan=professional").content.decode()
    assert "x-cloak" in _step_tag(html, "channels")
    assert "x-cloak" not in _step_tag(html, "account")


@pytest.mark.django_db
def test_buttons_that_start_hidden_stay_hidden_until_alpine(client, plans):
    html = client.get(SIGNUP_URL).content.decode()
    assert re.search(r'x-show="stepIndex > 0" x-cloak[^>]*>Back<', html)
    assert re.search(r'x-show="isLast\(\)" x-cloak>Create account<', html)


# ---- side panel and phone stepper -----------------------------------------------------------


def _channels(monkeypatch, *live):
    """Pin what public pages may say (accounts.views.marketing_channels) for one test."""
    from apps.accounts import views

    out = {
        k: "live" if k in live else ""
        for k in ("whatsapp", "instagram", "facebook", "email")
    }
    monkeypatch.setattr(views, "marketing_channels", lambda: out)


def _aside(client) -> str:
    html = client.get(SIGNUP_URL).content.decode()
    return html[html.index("<aside") : html.index("</aside>")]


@pytest.mark.django_db
def test_side_panel_sells_only_channels_that_are_on(client, plans, monkeypatch):
    _channels(monkeypatch, "whatsapp")
    aside = _aside(client)
    assert "hidden lg:block" in aside
    assert "Answer WhatsApp messages from one shared inbox." in aside
    assert "Instagram" not in aside
    assert 'data-claim="meta-tech-provider"' in aside
    _channels(monkeypatch, "whatsapp", "instagram", "facebook")
    assert "Answer WhatsApp, Instagram and Facebook Page messages" in _aside(client)


@pytest.mark.django_db
def test_side_panel_drops_chat_lines_when_no_chat_channel_is_on(
    client, plans, monkeypatch
):
    _channels(monkeypatch, "email")
    aside = _aside(client)
    assert "shared inbox" not in aside and "inbox.webp" not in aside
    assert "meta-tech-provider" not in aside
    assert 'data-claim="orders"' in aside


@pytest.mark.django_db
def test_phone_stepper_says_which_step_of_how_many(client, plans):
    html = client.get(SIGNUP_URL).content.decode()
    assert "'Step ' + (stepIndex + 1) + ' of ' + steps.length" in html
    assert 'class="hidden sm:flex' in html  # the labelled dots, from sm up


# ---- 404, 500, guessed addresses ------------------------------------------------------------


@pytest.mark.django_db
def test_404_is_branded_with_a_way_on(client):
    resp = client.get("/no-such-page-here/")
    assert resp.status_code == 404
    body = resp.content.decode()
    assert "find that page" in body
    assert 'href="/help/"' in body and 'href="/"' in body
    assert 'class="pub-header"' in body


def test_500_renders_with_no_request_and_no_context():
    source = (TEMPLATES / "500.html").read_text(encoding="utf-8")
    assert "{%" not in source and "{{" not in source
    html = Template(source).render(Context())
    assert "Something went wrong" in html and 'href="/"' in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    "guess, target",
    [
        ("/login/?next=/inbox/", "/auth/login/?next=/inbox/"),
        ("/register/", "/signup/"),
        ("/register/?plan=trial", "/signup/?plan=trial"),
    ],
)
def test_guessed_addresses_redirect_for_good(client, guess, target):
    resp = client.get(guess)
    assert resp.status_code == 301 and resp.url == target


# ---- autofill -------------------------------------------------------------------------------


def test_autofilled_fields_keep_the_theme_colours():
    built = (settings.BASE_DIR / "static" / "css" / "app.css").read_text(
        encoding="utf-8"
    )
    # Tailwind writes :-webkit-autofill as the standard :autofill.
    assert ".input:autofill" in built
    assert "-webkit-text-fill-color:var(--color-ink)" in built
