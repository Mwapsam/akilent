"""Landing page Phase 2: global content and trust (docs/plans/landing-premium-global.md).

- The positioning line is the default title and description, and the footer tagline.
- The trust section is two rows, Control and Security; every Security item opens its part of
  the Security page.
- /security/ states only what is true today (docs/ops/security-facts.md): no data region, no
  GDPR or DPA wording, a status page only when one is configured.
- Proof slots (logo row, case-study card) appear only with real, permissioned customers.
"""

import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.billing.models import Plan
from apps.core.models import SiteSettings, Testimonial

POSITIONING = "The customer conversation platform for teams that sell through chat"


@pytest.fixture
def plans(db):
    Plan.objects.create(slug=Plan.TRIAL, name="Trial", price_monthly=0, trial_days=14)
    Plan.objects.create(slug=Plan.PROFESSIONAL, name="Professional", price_monthly=49)


def _section(html, sid):
    return html.split(f'<section id="{sid}"', 1)[1].split("</section>", 1)[0]


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# ---- Global positioning ---------------------------------------------------------------------


@pytest.mark.django_db
def test_the_positioning_line_is_the_title_description_and_tagline(client, plans):
    html = client.get("/").content.decode()
    assert f"<title>{POSITIONING}" in html
    assert re.search(r'<meta name="description" content="[^"]*sell through chat', html)
    footer = html.split('<footer class="lp-footer', 1)[1]
    assert f"{POSITIONING}." in footer
    assert "small businesses" not in html
    # The company address stays, in the company block, not in the tagline.
    assert "Lusaka, Zambia" in footer


# ---- Trust ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_trust_is_two_rows_and_security_items_open_the_security_page(client, plans):
    trust = _section(client.get("/").content.decode(), "trust")
    assert 'id="trust-control"' in trust and 'id="trust-security"' in trust
    control, security = trust.split('id="trust-security"', 1)
    for claim in ("ai", "roles", "data-deletion"):
        assert f'data-claim="{claim}"' in control, claim
    for anchor in ("encryption", "hosting", "audit-log", "backups"):
        assert f'href="/security/#{anchor}"' in security, anchor
        assert f'data-claim="{anchor}"' in security, anchor


# ---- Security page -------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_security_page_covers_what_a_buyer_asks(client):
    resp = client.get("/security/")
    assert resp.status_code == 200
    html = resp.content.decode()
    for anchor in (
        "hosting",
        "encryption",
        "access",
        "audit-log",
        "backups",
        "incidents",
        "subprocessors",
        "disclosure",
    ):
        assert f'id="{anchor}"' in html, anchor
    for provider in (
        "Meta Platforms",
        "Amazon Web Services",
        "Cloudflare",
        "Email delivery provider",
        "PayPal",
    ):
        assert provider in html, provider
    # It sits with the legal pages and is listed in their navigation.
    assert 'aria-current="page">Security</a>' in html


@pytest.mark.django_db
def test_the_security_page_claims_nothing_unconfirmed(client):
    """docs/ops/security-facts.md: no region published, no GDPR or DPA yet, no coming soon."""
    text = _text(client.get("/security/").content.decode())
    for word in (
        "GDPR",
        "data processing agreement",
        "DPA",
        "coming soon",
        "SOC 2",
        "ISO 27001",
        "Ireland",
        "Frankfurt",
        "Virginia",
        "eu-west",
        "us-east",
    ):
        assert word.lower() not in text.lower(), word


@pytest.mark.django_db
def test_the_status_page_is_linked_only_when_there_is_one(client, settings, plans):
    settings.STATUS_PAGE_URL = ""
    assert 'id="status"' not in client.get("/security/").content.decode()
    assert ">Status</a>" not in client.get("/").content.decode()
    settings.STATUS_PAGE_URL = "https://status.example.com"
    assert (
        'href="https://status.example.com"' in client.get("/security/").content.decode()
    )
    assert (
        'href="https://status.example.com" rel="noopener">Status</a>'
        in client.get("/").content.decode()
    )
    assert (
        'href="https://status.example.com" rel="noopener">Status</a>'
        in client.get("/help/").content.decode()
    )


@pytest.mark.django_db
def test_vulnerabilities_have_a_contact(client, settings):
    settings.SECURITY_CONTACT_EMAIL = "security@example.com"
    assert "mailto:security@example.com" in client.get("/security/").content.decode()
    resp = client.get("/.well-known/security.txt")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Contact: mailto:security@example.com" in body
    assert re.search(r"^Expires: \d{4}-\d\d-\d\dT00:00:00Z$", body, re.M)
    assert "Policy: " in body and "/security/#disclosure" in body


@pytest.mark.django_db
def test_without_any_contact_there_is_no_security_txt(client, settings):
    settings.SECURITY_CONTACT_EMAIL = ""
    site = SiteSettings.load()
    site.support_email = ""
    site.save()
    assert client.get("/.well-known/security.txt").status_code == 404


def test_the_security_page_is_in_the_sitemap():
    from apps.seo.sitemaps import StaticPagesSitemap

    assert "security" in StaticPagesSitemap().items()


# ---- Proof slots ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_proof_shows_without_a_real_customer(client, plans):
    Testimonial.objects.create(
        quote="Draft",
        person_name="A",
        business_name="B",
        permission_note="",
        is_published=True,
    )
    html = client.get("/").content.decode()
    assert (
        'id="proof"' not in html
        and "lp-logos" not in html
        and "lp-proof-logos" not in html
    )


@pytest.mark.django_db
def test_logos_and_the_case_study_show_with_permission(
    client, plans, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    logo = SimpleUploadedFile(
        "logo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, content_type="image/png"
    )
    Testimonial.objects.create(
        quote="We answer every chat now.",
        person_name="Ada",
        business_name="Analytical Ltd",
        permission_note="Email from owner, 2026-11-07",
        is_published=True,
        logo=logo,
        case_study="They moved three phones into one inbox.",
    )
    html = client.get("/").content.decode()
    proof = _section(html, "proof")
    assert 'class="lp-logos"' in proof and 'alt="Analytical Ltd"' in proof
    assert (
        'class="lp-case' in proof and "They moved three phones into one inbox." in proof
    )
    # The case study's quote isn't repeated as a separate card.
    assert proof.count("We answer every chat now.") == 1
    assert 'class="lp-proof lp-proof-logos"' in _section(html, "hero")


@pytest.mark.django_db
def test_meta_tech_provider_is_stated_in_plain_text(client, plans, settings):
    """Owner-confirmed (docs/ops/security-facts.md). Meta publishes no Tech Provider badge and its
    brand rules forbid suggesting endorsement, so it's text only: no Meta or WhatsApp logo."""
    settings.WHATSAPP_ENABLED = True
    html = client.get("/").content.decode()
    for part in (_section(html, "hero"), _section(html, "trust")):
        assert 'data-claim="meta-tech-provider"' in part
        assert "Meta Tech Provider" in part
    footer = html.split('<footer class="lp-footer', 1)[1]
    assert "Meta Tech Provider" in footer
    assert not re.search(r"<img[^>]+(meta|whatsapp)[^>]*>", html, re.I)
    assert "Meta Tech Provider" in client.get("/security/").content.decode()
