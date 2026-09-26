"""Privacy, terms and data-deletion pages: public, linked from sign-up, and Meta can reach them."""
import pytest


@pytest.mark.django_db
@pytest.mark.parametrize("url, text", [
    ("/privacy/", "AI features"),
    ("/terms/", "WhatsApp Business Messaging Policy"),
    ("/data-deletion/", "Delete my data"),
])
def test_legal_pages_are_public(client, url, text):
    resp = client.get(url)
    assert resp.status_code == 200 and text in resp.content.decode()


@pytest.mark.django_db
def test_sign_up_links_to_the_terms_and_privacy_policy(client):
    html = client.get("/signup/").content.decode()
    assert 'href="/terms/"' in html and 'href="/privacy/"' in html
