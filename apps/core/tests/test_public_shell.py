"""Help, docs and legal pages use the public shell, signed in or not — never the dashboard."""

import pytest

PUBLIC_PAGES = [
    "/help/",
    "/help/team/",
    "/docs/",
    "/docs/channels/",
    "/privacy/",
    "/terms/",
    "/cookies/",
    "/data-deletion/",
]


@pytest.mark.django_db
@pytest.mark.parametrize("url", PUBLIC_PAGES)
def test_public_pages_render_without_the_dashboard_for_visitors(client, url):
    html = client.get(url).content.decode()
    assert 'class="pub-header"' in html
    assert 'name="akilent-shell"' not in html
    assert "Start free trial" in html


@pytest.mark.django_db
@pytest.mark.parametrize("url", PUBLIC_PAGES)
def test_public_pages_stay_public_when_signed_in(client, django_user_model, url):
    client.force_login(
        django_user_model.objects.create_user("ana", "ana@example.com", "pw")
    )
    html = client.get(url).content.decode()
    assert 'class="pub-header"' in html
    # No app shell: no sidebar nav, no command palette, just a way back in.
    assert 'name="akilent-shell"' not in html
    assert "Open dashboard" in html
    assert 'aria-label="Cookie notice"' not in html


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/help/", "/docs/", "/privacy/"])
def test_opening_from_the_app_does_a_full_page_load(client, django_user_model, url):
    # A boosted click from the dashboard must not swap the public page into the app shell.
    client.force_login(
        django_user_model.objects.create_user("ana", "ana@example.com", "pw")
    )
    resp = client.get(
        url, HTTP_HX_REQUEST="true", HTTP_HX_BOOSTED="true", HTTP_X_AKILENT_SHELL="app"
    )
    assert resp.status_code == 204
    assert resp["HX-Redirect"] == url


@pytest.mark.django_db
def test_public_footer_links_help_docs_and_every_legal_page(client):
    html = client.get("/help/").content.decode()
    for href in (
        "/help/",
        "/docs/",
        "/privacy/",
        "/terms/",
        "/cookies/",
        "/data-deletion/",
    ):
        assert f'href="{href}"' in html


@pytest.mark.django_db
def test_nav_marks_the_current_section(client):
    assert 'href="/help/" aria-current="page"' in client.get("/help/").content.decode()
    assert 'href="/docs/" aria-current="page"' in client.get("/docs/").content.decode()
