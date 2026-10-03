import pytest

from apps.seo.models import SEOPage


@pytest.mark.django_db
def test_api_catalog_rfc9727(client, settings):
    settings.SITE_URL = "https://akilent.com"
    resp = client.get("/.well-known/api-catalog")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/linkset+json"
    data = resp.json()
    assert "linkset" in data
    entry = data["linkset"][0]
    assert entry["anchor"] == "https://akilent.com/api/"
    assert any(
        d["href"] == "https://akilent.com/api/schema" for d in entry["service-desc"]
    )
    assert any(d["href"] == "https://akilent.com/docs/" for d in entry["service-doc"])
    assert any(d["href"] == "https://akilent.com/healthz" for d in entry["status"])


@pytest.mark.django_db
def test_robots_txt_when_indexing_disabled(client, settings):
    settings.SEO_ALLOW_INDEXING = False
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /" in r.text
    assert "Content-Signal: ai-train=no, search=yes, ai-input=no" in r.text


@pytest.mark.django_db
def test_robots_txt_when_indexing_enabled(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Sitemap:" in r.text
    assert "Disallow: /admin/" in r.text
    assert "Disallow: /dashboard/" in r.text
    assert "Allow: /" in r.text
    assert "Content-Signal: ai-train=no, search=yes, ai-input=no" in r.text


@pytest.mark.django_db
def test_sitemap_responds(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    r = client.get("/sitemap.xml")
    assert r.status_code == 200


@pytest.mark.django_db
def test_sitemap_excludes_app_paths(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    r = client.get("/sitemap.xml")
    assert "/dashboard/" not in r.text
    assert "/api/" not in r.text
    assert "/inbox/" not in r.text


@pytest.mark.django_db
def test_landing_link_headers_for_agent_discovery(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    resp = client.get("/")
    assert resp.status_code == 200
    link = resp.get("Link", "")
    assert 'rel="api-catalog"' in link
    assert "/.well-known/api-catalog" in link
    assert 'rel="service-desc"' in link
    assert "/api/schema" in link
    assert 'rel="service-doc"' in link
    assert "/docs/" in link
    assert 'rel="describedby"' in link


@pytest.mark.django_db
def test_landing_has_seo_tags(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    r = client.get("/")
    assert r.status_code == 200
    assert '<meta name="description"' in r.text
    assert 'property="og:title"' in r.text
    assert 'rel="canonical"' in r.text
    assert "application/ld+json" in r.text


@pytest.mark.django_db
def test_landing_canonical_excludes_query_string(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    settings.SITE_URL = "https://akilent.com"
    r = client.get("/?utm_source=test")
    assert 'href="https://akilent.com/"' in r.text


@pytest.mark.django_db
def test_og_image_is_absolute(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    settings.SITE_URL = "https://akilent.com"
    r = client.get("/")
    # og:image value should start with https://
    assert (
        'og:image" content="https://' in r.text or 'og:image" content="http' in r.text
    )


@pytest.mark.django_db
def test_seo_page_title_override(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    SEOPage.objects.create(
        path="/", title="Custom Akilent Title", description="Custom desc."
    )
    r = client.get("/")
    assert "Custom Akilent Title" in r.text
    assert "Custom desc." in r.text


@pytest.mark.django_db
def test_seo_page_blank_description_falls_back_to_default(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    settings.SEO_DEFAULTS = {
        "site_name": "Akilent",
        "default_title": "Default Title",
        "title_suffix": "— Akilent",
        "default_description": "The default description.",
        "og_image": "img/seo/og-default.png",
    }
    SEOPage.objects.create(path="/", title="Override Title", description="")
    r = client.get("/")
    assert "The default description." in r.text


@pytest.mark.django_db
def test_indexing_disabled_overrides_public_page(client, settings):
    settings.SEO_ALLOW_INDEXING = False
    r = client.get("/")
    assert "noindex" in r.text


@pytest.mark.django_db
def test_private_route_noindex(client, settings, django_user_model):
    settings.SEO_ALLOW_INDEXING = True
    user = django_user_model.objects.create_user(
        username="u@example.com",
        password="test-pass-123",  # noqa: S106
    )
    client.force_login(user)
    r = client.get("/dashboard/")
    assert "noindex" in r.text


@pytest.mark.django_db
def test_private_route_cannot_become_indexable(client, settings, django_user_model):
    """A SEOPage override with noindex=False must not make a private route indexable."""
    settings.SEO_ALLOW_INDEXING = True
    SEOPage.objects.create(path="/dashboard/", noindex=False)
    user = django_user_model.objects.create_user(
        username="u2@example.com",
        password="test-pass-123",  # noqa: S106
    )
    client.force_login(user)
    r = client.get("/dashboard/")
    assert "noindex,nofollow" in r.text


@pytest.mark.django_db
def test_seopage_noindex_clears_sitemap_flag():
    page = SEOPage.objects.create(path="/test/", noindex=True, sitemap=True)
    page.refresh_from_db()
    assert page.sitemap is False
