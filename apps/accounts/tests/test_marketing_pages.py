"""Product, integration and pricing pages (apps/accounts/marketing_pages.py): each is its own
indexable page with its own title and description, open only while what it sells is on, linked
from the header and listed in the sitemap."""

import json
import re

import pytest

from apps.accounts import marketing_pages as mp
from apps.billing.models import Plan
from apps.core import channels as ch
from apps.core.models import SiteSettings

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _setup(settings, db):
    settings.SEO_ALLOW_INDEXING = True
    settings.SITE_URL = "https://akilent.com"
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    settings.FACEBOOK_ENABLED = True
    settings.EMAIL_ENABLED = True
    site = SiteSettings.load()
    site.restricted_channels = []
    site.save()
    ch.invalidate_channel_cache()
    Plan.objects.create(slug=Plan.TRIAL, name="Trial", price_monthly=0, trial_days=14)
    Plan.objects.create(slug=Plan.PROFESSIONAL, name="Professional", price_monthly=49)
    yield
    ch.invalidate_channel_cache()


def _paths(client):
    xml = client.get("/sitemap.xml").content.decode()
    return [
        re.sub(r"^https?://[^/]+", "", loc) for loc in re.findall(r"<loc>([^<]+)</loc>", xml)
    ]


def _head(html):
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip()
    desc = re.search(r'<meta name="description" content="([^"]*)"', html).group(1)
    return title, desc


def _json_ld(html):
    return [
        json.loads(block)
        for block in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S
        )
    ]


def test_registry_titles_and_descriptions_fit_search_results():
    titles = [p.title for p in mp.PAGES]
    assert len(set(titles)) == len(titles)
    for page in mp.PAGES:
        # Google shows about 160 characters of a description and 60 of a title.
        assert len(page.description) <= 160, page.slug
        assert len(page.title) <= 60, page.slug


def test_every_page_in_the_sitemap_has_its_own_title_and_description(client):
    paths = _paths(client)
    for expected in (
        "/pricing/",
        "/product/",
        "/product/shared-inbox/",
        "/product/post-results/",
        "/integrations/",
        "/integrations/whatsapp/",
    ):
        assert expected in paths
    titles, descriptions = {}, {}
    for path in paths:
        resp = client.get(path)
        assert resp.status_code == 200, path
        title, desc = _head(resp.content.decode())
        titles.setdefault(title, []).append(path)
        descriptions.setdefault(desc, []).append(path)
    assert not {t: p for t, p in titles.items() if len(p) > 1}
    assert not {d: p for d, p in descriptions.items() if len(p) > 1}


def test_a_page_for_a_channel_that_is_off_is_closed(client, settings):
    settings.WHATSAPP_ENABLED = False
    ch.invalidate_channel_cache()
    assert client.get("/integrations/whatsapp/").status_code == 404
    assert client.get("/product/post-results/").status_code == 404  # needs the chat link
    paths = _paths(client)
    assert "/integrations/whatsapp/" not in paths
    assert "/product/post-results/" not in paths
    assert "/integrations/instagram/" in paths
    assert client.get("/product/unknown/").status_code == 404


def test_moved_use_case_pages_redirect_permanently(client):
    resp = client.get("/for/shared-inbox/")
    assert resp.status_code == 301
    assert resp["Location"] == "/product/shared-inbox/"


def test_product_page_has_breadcrumbs_and_faq(client):
    html = client.get("/product/shared-inbox/").content.decode()
    crumbs = next(i for i in _json_ld(html) if i["@type"] == "BreadcrumbList")
    assert [c["item"] for c in crumbs["itemListElement"]] == [
        "https://akilent.com/",
        "https://akilent.com/product/",
        "https://akilent.com/product/shared-inbox/",
    ]
    assert 'aria-label="Breadcrumb"' in html
    assert 'itemtype="https://schema.org/FAQPage"' in html
    assert html.count("<h1") == 1
    # Related pages link onward, so every page is reachable from the others.
    assert 'href="/product/automations/"' in html


def test_pricing_page(client):
    html = client.get("/pricing/").content.decode()
    title, desc = _head(html)
    assert title == "Pricing — Akilent"
    assert "<h1" in html and "Professional" in html
    types = {i["@type"] for i in _json_ld(html)}
    assert {"SoftwareApplication", "BreadcrumbList"} <= types
    assert 'itemtype="https://schema.org/FAQPage"' in html


def test_help_articles_use_their_own_summary(client):
    from apps.core import help as help_kb

    article = help_kb.visible_articles()[0]
    html = client.get(f"/help/{article.slug}/").content.decode()
    _, desc = _head(html)
    assert desc == article.summary.replace("'", "&#x27;").replace('"', "&quot;")


@pytest.mark.parametrize("url", ["/", "/help/", "/pricing/", "/product/sales/"])
def test_header_and_footer_link_to_the_pages(client, url):
    html = client.get(url).content.decode()
    for href in ('href="/product/"', 'href="/integrations/"', 'href="/pricing/"'):
        assert href in html, (url, href)
    assert 'href="/integrations/whatsapp/"' in html  # footer
