"""Privacy, terms and data-deletion pages: public, linked from sign-up, and Meta can reach them."""

import pytest


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url, text",
    [
        ("/privacy/", "AI features"),
        ("/terms/", "WhatsApp Business Messaging Policy"),
        ("/data-deletion/", "Delete my data"),
    ],
)
def test_legal_pages_are_public(client, url, text):
    resp = client.get(url)
    assert resp.status_code == 200 and text in resp.content.decode()


@pytest.mark.django_db
def test_sign_up_links_to_the_terms_and_privacy_policy(client):
    html = client.get("/signup/").content.decode()
    assert 'href="/terms/"' in html and 'href="/privacy/"' in html


@pytest.mark.django_db
def test_data_deletion_status_shows_the_confirmation_code(client):
    html = client.get("/data-deletion/?code=0123456789abcdef").content.decode()
    assert "0123456789abcdef" in html and "request was received" in html


@pytest.mark.django_db
def test_data_deletion_status_ignores_a_code_we_did_not_issue(client):
    html = client.get("/data-deletion/?code=<script>alert(1)</script>").content.decode()
    assert "request was received" not in html


@pytest.mark.django_db
def test_privacy_and_deletion_pages_cover_instagram(client):
    assert "Instagram" in client.get("/privacy/").content.decode()
    assert "Disconnecting Instagram" in client.get("/data-deletion/").content.decode()
