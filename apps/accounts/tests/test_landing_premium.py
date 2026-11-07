"""Landing page, premium and global (docs/plans/landing-premium-global.md, Phases 0 and 1).

- The logged-out page is cacheable at Cloudflare; a signed-in page, or one showing a message,
  never is.
- Every address emails and ads may link to still lands somewhere.
- The hero has one amber action, and calls to action carry their analytics names.
- The main story names no country's currency, people or payment method.
- "Book a demo" is one real form that saves the request and emails sales, without JavaScript.
- The page stays inside its byte budgets: its own small stylesheet, a deferred script.
"""

import gzip
import re
from pathlib import Path
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.messages import constants
from django.contrib.messages.storage.fallback import FallbackStorage

from apps.accounts.models import DemoRequest
from apps.billing.models import Plan

STATIC = Path(settings.BASE_DIR) / "static"
LANDING = Path(settings.BASE_DIR) / "templates" / "accounts" / "landing"


@pytest.fixture
def plans(db):
    Plan.objects.create(slug=Plan.TRIAL, name="Trial", price_monthly=0, trial_days=14)
    Plan.objects.create(slug=Plan.PROFESSIONAL, name="Professional", price_monthly=49)


def _section(html, sid):
    return html.split(f'<section id="{sid}"', 1)[1].split("</section>", 1)[0]


# ---- Caching -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_logged_out_page_is_cacheable_at_the_edge(client, plans):
    resp = client.get("/")
    cc = resp["Cache-Control"]
    assert "public" in cc and "s-maxage=600" in cc and "max-age=0" in cc
    assert "private" not in cc and "no-store" not in cc
    html = resp.content.decode()
    assert 'href="/auth/login/" class="lp-signin">Sign in' in html
    assert "Open dashboard" not in html
    # Nothing on it sets a cookie that would make the cached copy someone's.
    assert not resp.cookies


@pytest.mark.django_db
def test_a_signed_in_page_is_never_stored(client, plans):
    user = User.objects.create_user("owner", "owner@example.com", "pw-123456789")
    client.force_login(user)
    resp = client.get("/")
    cc = resp["Cache-Control"]
    assert "private" in cc and "no-store" in cc and "public" not in cc
    html = resp.content.decode()
    assert "Open dashboard" in html and "Sign in" not in html


@pytest.mark.django_db
def test_a_page_showing_a_message_is_never_stored(rf, plans):
    """ "You've been signed out" belongs to one visitor, so that page isn't cached."""
    from django.contrib.auth.models import AnonymousUser
    from django.contrib.sessions.middleware import SessionMiddleware

    from apps.accounts.views import landing

    request = rf.get("/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.user = AnonymousUser()
    request._messages = FallbackStorage(request)
    request._messages.add(constants.INFO, "You've been signed out.")
    resp = landing(request)
    assert "no-store" in resp["Cache-Control"]
    assert "You&#x27;ve been signed out." in resp.content.decode()


# ---- Addresses -----------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "anchor",
    [
        "channels",
        "how-it-works",
        "platform",
        "features",
        "pricing",
        "faq",
        "developers",
    ],
)
def test_every_old_address_still_lands(client, plans, anchor):
    assert f'id="{anchor}"' in client.get("/").content.decode()


@pytest.mark.django_db
def test_content_studio_address_is_its_panel_when_publishing_is_on(
    client, settings, plans
):
    settings.INSTAGRAM_ENABLED = True
    from apps.core import channels as ch

    ch.invalidate_channel_cache()
    html = client.get("/").content.decode()
    assert '<div class="lp-panel" id="content-studio"' in html
    # Without JavaScript the tab row stays hidden and every panel shows.
    assert '<div class="lp-ptabs" role="tablist" aria-label="Platform" hidden>' in html
    assert not re.search(r'class="lp-panel"[^>]*\shidden', html)


# ---- Calls to action -----------------------------------------------------------------------


@pytest.mark.django_db
def test_the_hero_has_one_amber_action_and_a_demo(client, plans):
    hero = _section(client.get("/").content.decode(), "hero")
    assert hero.count("lp-btn-primary") == 1
    assert 'data-cta="start_trial" data-cta-location="hero"' in hero
    assert 'href="/demo/" class="lp-btn lp-btn-ghost" data-cta="book_demo"' in hero
    # The headline draws in the first paint: nothing in the hero waits for a reveal.
    assert "lp-reveal" not in hero


@pytest.mark.django_db
def test_every_call_to_action_is_named_for_analytics(client, plans):
    html = client.get("/").content.decode()
    for a in re.findall(r'<a [^>]*href="(?:/signup/[^"]*|/demo/)"[^>]*>', html):
        assert "data-cta=" in a and "data-cta-location=" in a, a
    assert (
        'data-cta="plan" data-cta-plan="professional" data-cta-location="pricing"'
        in html
    )
    js = (STATIC / "js" / "landing.js").read_text(encoding="utf-8")
    assert "gtag('event', 'cta_click'" in js


# ---- Global story --------------------------------------------------------------------------

LOCAL = re.compile(r"\bK\d|Kwacha|MoMo|\bMTN\b|Thandi|Kitwe|Mwila")


@pytest.mark.parametrize(
    "name",
    [
        "_hero.html",
        "_journey.html",
        "_platform.html",
        *[f"art/{p.name}" for p in (LANDING / "art").glob("*.html")],
    ],
)
def test_the_story_names_no_country_currency_people_or_payment(name):
    text = (LANDING / name).read_text(encoding="utf-8")
    assert not LOCAL.search(text), LOCAL.search(text).group(0)


# ---- Book a demo ---------------------------------------------------------------------------


def _demo_post(client, **extra):
    data = {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "company": "Analytical Ltd",
        "team_size": "6-20",
        "channels": ["whatsapp", "instagram"],
        "message": "A walk through the inbox, please.",
    }
    data.update(extra)
    return client.post("/demo/", data)


@pytest.mark.django_db
def test_the_demo_page_renders_without_javascript(client):
    resp = client.get("/demo/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'method="post"' in html and "csrfmiddlewaretoken" in html
    assert 'hx-boost="false"' in html
    assert "no-store" in resp["Cache-Control"]


@pytest.mark.django_db
def test_a_demo_request_is_saved_and_sent_to_sales(client, settings):
    settings.SALES_NOTIFY_EMAIL = "sales@example.com"
    with mock.patch("apps.email.services.send.send_system_email") as send:
        resp = _demo_post(client)
    assert resp.status_code == 302 and resp["Location"].endswith("/demo/?sent=1")
    req = DemoRequest.objects.get()
    assert req.company == "Analytical Ltd" and req.channels == ["whatsapp", "instagram"]
    to, subject = send.call_args.args[:2]
    assert to == "sales@example.com" and "Analytical Ltd" in subject
    assert "WhatsApp, Instagram" in send.call_args.kwargs["text_body"]
    assert (
        "Thanks, we've got your request" in client.get("/demo/?sent=1").content.decode()
    )


@pytest.mark.django_db
def test_a_demo_request_is_kept_when_the_email_fails(client, settings):
    settings.SALES_NOTIFY_EMAIL = "sales@example.com"
    with mock.patch(
        "apps.email.services.send.send_system_email",
        side_effect=RuntimeError("smtp down"),
    ):
        resp = _demo_post(client)
    assert resp.status_code == 302
    assert DemoRequest.objects.count() == 1


@pytest.mark.django_db
def test_the_demo_form_says_what_is_missing(client):
    resp = _demo_post(client, email="not-an-email", company="")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'id="err-email"' in html and 'id="err-company"' in html
    assert 'value="Ada Lovelace"' in html  # what was typed stays
    assert DemoRequest.objects.count() == 0


@pytest.mark.django_db
def test_bots_filling_the_honeypot_are_thanked_and_dropped(client):
    with mock.patch("apps.email.services.send.send_system_email") as send:
        resp = _demo_post(client, website="https://spam.example")
    assert resp.status_code == 302
    assert DemoRequest.objects.count() == 0 and not send.called


@pytest.mark.django_db
def test_demo_requests_are_limited_per_address(client, settings):
    from django.core.cache import cache

    from apps.accounts.views import DEMO_REQUESTS_PER_HOUR

    cache.clear()
    settings.SALES_NOTIFY_EMAIL = ""
    for _ in range(DEMO_REQUESTS_PER_HOUR):
        assert _demo_post(client).status_code == 302
    resp = _demo_post(client)
    assert resp.status_code == 200 and "Too many requests" in resp.content.decode()
    assert DemoRequest.objects.count() == DEMO_REQUESTS_PER_HOUR


def test_the_demo_page_is_in_the_sitemap():
    from apps.seo.sitemaps import StaticPagesSitemap

    assert "demo" in StaticPagesSitemap().items()


# ---- Budgets -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_page_stays_inside_its_byte_budgets(client, plans):
    html = client.get("/").content
    assert len(gzip.compress(html)) < 60_000
    head = html.decode().split("</head>", 1)[0]
    assert "css/app.css" not in head  # the dashboard's stylesheet is not this page's
    # No script holds up the first paint: the page's own is deferred, GA loads on idle.
    for tag in re.findall(r"<script[^>]*src=[^>]*>", head):
        assert " defer" in tag or " async" in tag, tag
    assert (STATIC / "js" / "landing.js").stat().st_size < 50_000
    css = (STATIC / "css" / "landing.css").read_bytes()
    assert len(gzip.compress(css)) < 16_000
