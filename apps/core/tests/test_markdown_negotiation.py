"""Markdown content negotiation — Accept: text/markdown returns clean Markdown."""

import pytest

ACCEPT_MD = {"HTTP_ACCEPT": "text/markdown"}


@pytest.mark.django_db
def test_help_index_returns_markdown(client):
    resp = client.get("/help/", **ACCEPT_MD)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/markdown")
    assert "# Help Center" in resp.text
    assert "Getting started" in resp.text


@pytest.mark.django_db
def test_help_article_returns_markdown(client):
    resp = client.get("/help/getting-started/", **ACCEPT_MD)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/markdown")
    text = resp.text
    assert "# Getting started" in text
    # Body content should be present in Markdown form.
    assert "## " in text  # at least one heading converted
    assert "<p>" not in text  # no raw HTML tags


@pytest.mark.django_db
def test_help_article_vary_header(client):
    resp = client.get("/help/getting-started/", **ACCEPT_MD)
    assert "Accept" in resp.get("Vary", "")


@pytest.mark.django_db
def test_legal_page_returns_markdown(client, settings):
    settings.SEO_ALLOW_INDEXING = True
    resp = client.get("/privacy/", **ACCEPT_MD)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/markdown")
    text = resp.text
    assert "# Privacy policy" in text
    assert "<style>" not in text
    assert "Last updated" in text


@pytest.mark.django_db
def test_docs_page_returns_markdown(client):
    resp = client.get("/docs/authentication/", **ACCEPT_MD)
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/markdown")
    text = resp.text
    assert "# Authentication" in text
    assert "## " in text
    assert "<div" not in text


@pytest.mark.django_db
def test_html_is_default_when_accept_not_set(client):
    resp = client.get("/help/getting-started/")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")


@pytest.mark.django_db
def test_html_to_markdown_basic():
    from apps.core.markdown import html_to_markdown

    html = "<h2>Title</h2><p>Hello <strong>world</strong>.</p>"
    md = html_to_markdown(html)
    assert "## Title" in md
    assert "**world**" in md
    assert "<h2>" not in md
