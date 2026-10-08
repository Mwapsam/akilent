import pytest
from django.utils.html import escape

from apps.core import docs as docs_kb


@pytest.fixture(autouse=True)
def all_channels(settings):
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    settings.EMAIL_ENABLED = True


@pytest.mark.django_db
def test_docs_index_renders_for_anonymous(client):
    resp = client.get("/docs/")
    assert resp.status_code == 200
    assert b"Developer Platform" in resp.content


@pytest.mark.django_db
@pytest.mark.parametrize("page", docs_kb.PAGES, ids=lambda p: p.slug)
def test_every_docs_page_renders(client, page):
    url = "/docs/" if page.slug == "index" else f"/docs/{page.slug}/"
    resp = client.get(url)
    assert resp.status_code == 200
    assert escape(page.title).encode() in resp.content


@pytest.mark.django_db
def test_unknown_docs_page_is_404(client):
    assert client.get("/docs/does-not-exist/").status_code == 404


@pytest.mark.django_db
def test_docs_nav_lists_every_page(client):
    resp = client.get("/docs/")
    for page in docs_kb.PAGES:
        assert escape(page.title).encode() in resp.content


@pytest.mark.django_db
def test_docs_sidebar_shows_section_labels(client):
    resp = client.get("/docs/")
    for label in ("Getting started", "API reference", "Platform"):
        assert escape(label).encode() in resp.content


def test_sections_are_contiguous():
    # {% regroup %} only groups consecutive items, so a section must never
    # reappear after a different one.
    seen = []
    for page in docs_kb.PAGES:
        if page.section not in seen:
            seen.append(page.section)
        else:
            assert page.section == seen[-1], (
                f"Section {page.section!r} is not contiguous in PAGES"
            )
    assert seen == ["Getting started", "API reference", "Platform"]


def test_neighbors():
    first, last = docs_kb.PAGES[0], docs_kb.PAGES[-1]
    assert docs_kb.neighbors(first) == (None, docs_kb.PAGES[1])
    assert docs_kb.neighbors(last) == (docs_kb.PAGES[-2], None)


@pytest.mark.django_db
def test_prev_next_pagination_renders(client):
    resp = client.get("/docs/authentication/")
    assert b"/docs/messages/" in resp.content  # next
    assert b'rel="prev"' in resp.content
    assert b'rel="next"' in resp.content
    # First page has no "previous"
    resp = client.get("/docs/")
    assert b'rel="prev"' not in resp.content
    assert b'rel="next"' in resp.content


@pytest.mark.django_db
def test_smtp_page_shows_relay_host_and_port(client, settings):
    settings.SMTP_RELAY_HOST = "mail.example.com"
    settings.SMTP_RELAY_PORT = 587
    resp = client.get("/docs/smtp/")
    assert resp.status_code == 200
    assert b"mail.example.com" in resp.content
    assert b"587" in resp.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "page", [p for p in docs_kb.PAGES if p.channel], ids=lambda p: p.slug
)
def test_disabled_channel_hides_its_docs(client, settings, page):
    setattr(settings, f"{page.channel.upper()}_ENABLED", False)
    assert client.get(f"/docs/{page.slug}/").status_code == 404
    assert f'href="/docs/{page.slug}/"'.encode() not in client.get("/docs/").content
