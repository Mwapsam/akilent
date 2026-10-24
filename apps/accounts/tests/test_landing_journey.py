"""The public site sells the whole journey by its features: every channel that's on is
sold by what it does (no app-review talk), a channel that's off isn't mentioned, proof
appears only from permissioned testimonials, and use-case pages follow the same rules."""

import pytest
from django.contrib.auth.models import User

from apps.billing.models import ComingSoonFeature, Plan
from apps.core import channels as ch
from apps.core.models import SiteSettings, Testimonial

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _setup(settings, db):
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    settings.FACEBOOK_ENABLED = True
    settings.EMAIL_ENABLED = True
    ch.invalidate_channel_cache()
    Plan.objects.create(slug=Plan.TRIAL, name="Trial", price_monthly=0, trial_days=14)
    yield
    ch.invalidate_channel_cache()


def _open_meta():
    site = SiteSettings.load()
    site.restricted_channels = []
    site.save()


def _html(client, url="/"):
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


@pytest.mark.parametrize("meta_open", [False, True])
def test_every_channel_that_is_on_is_sold_by_its_features(client, meta_open):
    # Whether Instagram and Facebook are offered at signup yet doesn't change the
    # marketing: the page sells what they do and never talks about Meta's review.
    if meta_open:
        _open_meta()
    html = _html(client)
    assert "WhatsApp and Instagram messages" in html
    for gone in ("Opening soon", "waiting for Meta", "Meta approves", "Meta is still reviewing"):
        assert gone not in html
    for step in ("Attract", "Engage", "Convert", "Measure"):
        assert step in html
    assert "See which post brought the sale" in html
    assert "img/marketing/post-results.webp" in html
    assert 'id="content-studio"' in html


def test_a_switched_off_channel_is_not_mentioned(client, settings):
    settings.FACEBOOK_ENABLED = False
    ch.invalidate_channel_cache()
    html = _html(client)
    assert (
        "Facebook Page"
        not in html.split('id="channels"', 1)[1].split("</section>", 1)[0]
    )


def test_screenshots_have_dimensions_and_alt_text(client):
    import re

    _open_meta()
    html = _html(client)
    for tag in re.findall(r"<img [^>]*>", html):
        if "img/marketing/" in tag or "img/demo/" in tag:
            assert 'alt="' in tag and 'alt=""' not in tag, tag
            assert "width=" in tag and "height=" in tag, tag


def test_proof_shows_only_published_permissioned_testimonials(client):
    assert 'id="proof"' not in _html(client)
    Testimonial.objects.create(
        quote="Unpublished words", person_name="A", permission_note="ok"
    )
    assert 'id="proof"' not in _html(client)
    Testimonial.objects.create(
        quote="We answer every enquiry now.",
        person_name="Mwila",
        business_name="Shop",
        permission_note="Email from owner, 2026-11-02",
        is_published=True,
    )
    html = _html(client)
    assert 'id="proof"' in html and "We answer every enquiry now." in html
    assert "Unpublished words" not in html


def test_coming_soon_lists_only_active_entries(client):
    assert 'id="coming-soon"' not in _html(client)
    ComingSoonFeature.objects.create(name="Website chat", is_active=True)
    ComingSoonFeature.objects.create(name="Hidden thing", is_active=False)
    html = _html(client)
    assert "Website chat" in html and "Hidden thing" not in html


def test_use_case_pages_follow_the_channels(client, settings):
    html = _html(client, "/for/missed-enquiries/")
    assert 'href="/signup/?channels=whatsapp,instagram"' in html
    studio = _html(client, "/for/content-studio/")
    assert 'href="/signup/?channels=instagram,facebook"' in studio
    assert "Meta" not in studio.split("<main", 1)[-1].split("</main>", 1)[0]

    settings.WHATSAPP_ENABLED = False
    ch.invalidate_channel_cache()
    assert client.get("/for/missed-enquiries/").status_code == 404
    assert client.get("/for/unknown/").status_code == 404


def test_sitemap_lists_open_use_case_pages(client):
    xml = client.get("/sitemap.xml").content.decode()
    assert "/for/missed-enquiries/" in xml and "/for/content-studio/" in xml


def test_publishing_a_testimonial_needs_a_permission_note(client):
    admin = User.objects.create_superuser("ops", "ops@example.com", "pw-123456!")
    client.force_login(admin)
    data = {"quote": "Great", "person_name": "A", "is_published": "on"}
    client.post("/manage/testimonials/save/", data)
    assert not Testimonial.objects.exists()
    client.post(
        "/manage/testimonials/save/", {**data, "permission_note": "Owner agreed"}
    )
    assert Testimonial.objects.get().is_published
    assert client.get("/manage/testimonials/").status_code == 200


def test_help_follows_the_channels(client, settings):
    from apps.core import help as help_kb

    slugs = {a.slug for a in help_kb.visible_articles()}
    assert {"content-studio", "facebook-page-setup", "getting-started"} <= slugs
    settings.FACEBOOK_ENABLED = False
    settings.EMAIL_ENABLED = False
    ch.invalidate_channel_cache()
    slugs = {a.slug for a in help_kb.visible_articles()}
    assert "facebook-page-setup" not in slugs
    # Getting started no longer depends on email.
    assert "getting-started" in slugs
    html = _html(client, "/help/getting-started/")
    assert "Add &amp; configure a sending domain" not in html
