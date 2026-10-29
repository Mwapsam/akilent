"""The redesigned landing page (2026-10-29): illustrations, not screenshots, and labelled as such.

The product pictures are drawn in HTML/CSS (templates/accounts/landing/art/). They must say
they're illustrations, describe themselves to screen readers, and only ever show channels that
are switched on. The design lives in static/css/landing.css.
"""

import re
from decimal import Decimal

import pytest

from apps.billing.models import Plan


@pytest.fixture
def plans(db):
    Plan.objects.create(slug=Plan.TRIAL, name="Trial", price_monthly=0, trial_days=14)
    Plan.objects.create(slug=Plan.PROFESSIONAL, name="Professional", price_monthly=49)


def _html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.content.decode()


def _pricing(html):
    return html.split('<section id="pricing"', 1)[1].split("</section>", 1)[0]


def _figures(html):
    return re.findall(
        r'<figure class="lp-art[^"]*" data-art="([a-z]+)"(.*?)</figure>', html, re.S
    )


@pytest.mark.django_db
@pytest.mark.parametrize("whatsapp", [True, False])
def test_every_illustration_is_labelled_and_described(
    client, settings, plans, whatsapp
):
    settings.WHATSAPP_ENABLED = whatsapp
    figures = _figures(_html(client))
    assert figures
    for name, body in figures:
        assert 'role="img"' in body and 'aria-label="Illustration' in body, name
        assert "Illustration" in body.split("<figcaption", 1)[1], name
        # Sample chat text inside isn't markup the claim register reads as a product claim.
        inner = body.split('aria-hidden="true"', 1)[1]
        assert not re.search(r"<(p|li)[\s>]", inner), name


@pytest.mark.django_db
def test_illustrations_only_show_channels_that_are_on(client, settings, plans):
    settings.WHATSAPP_ENABLED = False
    html = _html(client)
    for _name, body in _figures(html):
        assert "WhatsApp" not in body
    # Nor anywhere else on the page (the Instagram card used to say "same inbox as WhatsApp").
    assert "WhatsApp" not in html


def _only(settings, *on):
    from apps.core import channels as ch

    for key in ("whatsapp", "instagram", "facebook", "email"):
        setattr(settings, f"{key.upper()}_ENABLED", key in on)
    ch.invalidate_channel_cache()


def _hero(html):
    return html.split('<section id="hero"', 1)[1].split("</section>", 1)[0]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "on, absent",
    [
        (("instagram", "facebook"), ["WhatsApp", "Email", "email", "@"]),
        (("instagram",), ["WhatsApp", "Facebook", "Email", "email"]),
        (("email",), ["WhatsApp", "Instagram", "Facebook", "sell through chats"]),
        ((), ["WhatsApp", "Instagram", "Facebook", "Email", "email", "chats"]),
    ],
)
def test_the_hero_names_only_channels_that_are_on(client, settings, plans, on, absent):
    """Badge, lead and the customer-record illustration all follow the channels."""
    _only(settings, *on)
    hero = _hero(_html(client))
    assert 'data-art="customer"' in hero
    for word in absent:
        assert word not in hero, word


@pytest.mark.django_db
def test_the_hero_badge_is_a_registered_claim(client, settings, plans):
    _only(settings, "whatsapp")
    hero = _hero(_html(client))
    assert '<span class="lp-badge" data-claim="lost-enquiries">' in hero


@pytest.mark.django_db
def test_page_classes_and_motion_stay_safe(client, plans):
    html = _html(client)
    # lp- classes only, so app.css's .nav-link hover and focus styles don't leak in.
    assert "nav-link" not in html
    # Help stays in the desktop header, as before the redesign.
    primary = html.split('aria-label="Primary"', 1)[1].split("</nav>", 1)[0]
    assert 'href="/help/"' in primary
    # The hidden reveal state is switched on by the script that reveals it, not in <head>.
    assert "classList.add('js')" not in html.split("</head>", 1)[0]
    # Reveal delays only use the classes landing.css defines.
    assert not re.search(r"lp-reveal[^\"]* d([4-9]|\d\d)\b", html)


@pytest.mark.django_db
def test_no_screenshots_and_no_coming_soon_strip(client, plans):
    html = _html(client)
    assert "img/marketing/" not in html and "img/demo/" not in html
    assert 'id="coming-soon"' not in html


@pytest.mark.django_db
def test_the_design_is_its_own_stylesheet(client, plans):
    html = _html(client)
    assert "css/landing.css" in html
    assert "<style" not in html
    assert 'class="lp"' in html


@pytest.mark.django_db
def test_plan_prices_are_exact_not_rounded(client, settings, plans):
    """$11.90 used to show as $12 (floatformat:0). Prices match the billing Plans page."""
    settings.BILLING_CURRENCY = "USD"
    for slug, price in (("starter", "11.9"), ("growth", "25.9")):
        Plan.objects.create(slug=slug, name=slug.title(), price_monthly=Decimal(price))
    Plan.objects.filter(slug=Plan.PROFESSIONAL).update(price_monthly=Decimal("49.9"))
    pricing = _pricing(_html(client))
    for shown in ("$11.90", "$25.90", "$49.90"):
        assert shown in pricing, shown
    for rounded in ("$12<", "$26<", "$50<"):
        assert rounded not in pricing, rounded


@pytest.mark.django_db
def test_plan_prices_follow_the_billing_currency(client, settings, plans):
    """Plans are priced in BILLING_CURRENCY, so the page never shows them as dollars."""
    settings.BILLING_CURRENCY = "ZMW"
    pricing = _pricing(_html(client))
    assert "K49.00" in pricing and "$" not in pricing


def _journey(html):
    return html.split('<section id="how-it-works"', 1)[1].split("</section>", 1)[0]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "on, steps, absent",
    [
        (
            ("whatsapp", "instagram", "facebook", "email"),
            ["attract", "engage", "convert", "measure"],
            [],
        ),
        (
            ("instagram", "facebook", "email"),
            ["attract", "engage", "convert", "measure"],
            ["WhatsApp", "chat link"],
        ),
        (
            ("facebook",),
            ["attract", "engage", "convert", "measure"],
            ["WhatsApp", "Instagram"],
        ),
        (
            ("email",),
            ["reach", "convert", "measure"],
            ["WhatsApp", "Instagram", "Facebook"],
        ),
        ((), ["convert", "measure"], ["WhatsApp", "Instagram", "Facebook", "email"]),
    ],
)
def test_the_journey_follows_the_channels(client, settings, plans, on, steps, absent):
    """One illustrated timeline in every setup: the right steps, in order, and only channels
    that are on. Numbers come from a CSS counter, so a hidden step leaves no gap."""
    _only(settings, *on)
    journey = _journey(_html(client))
    shown = re.findall(r'<li class="lp-step[^"]*" id="step-([a-z]+)"', journey)
    assert shown == steps
    for word in absent:
        assert word not in journey, word
    for name, body in _figures(journey):
        assert 'aria-label="Illustration' in body, name
    # The step map in the heading links to exactly the steps shown.
    assert re.findall(r'href="#step-([a-z]+)"', journey) == steps


def _studio(html):
    return html.split('<section id="content-studio"', 1)[1].split("</section>", 1)[0]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "on, measure, absent",
    [
        (("whatsapp", "instagram", "facebook"), "post-chat-link post-results", []),
        (("instagram", "facebook"), "post-metrics", ["WhatsApp", "chat link"]),
        (("instagram",), "post-metrics", ["WhatsApp", "Facebook", "2 versions"]),
        (("facebook",), "post-metrics", ["WhatsApp", "Instagram", "2 versions"]),
    ],
)
def test_content_studio_backs_its_headline_and_follows_the_channels(
    client, settings, plans, on, measure, absent
):
    """The measure card backs "measurable results" (post results with the WhatsApp chat
    link, else reach and engagement), and the composer names only channels that are on."""
    _only(settings, *on)
    studio = _studio(_html(client))
    card = studio.split('class="lp-sfeat measure"', 1)[1].split("</li>", 1)[0]
    assert f'data-claim="{measure}"' in card
    for word in absent:
        assert word not in studio, word
    if "instagram" in on and "facebook" in on:
        assert "2 versions" in studio
    # One time per channel that's on.
    assert len(re.findall(r'<div><i class="(ig|fb)"></i>', studio)) == len(
        {"instagram", "facebook"} & set(on)
    )
    assert 'aria-label="Illustration' in studio
