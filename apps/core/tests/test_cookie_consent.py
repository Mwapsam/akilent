"""Google Analytics waits for consent: the loader only fetches gtag.js after Accept, and every
public shell carries the Accept / Decline bar."""

import pytest

LOADER = "googletagmanager.com/gtag/js"


@pytest.fixture
def ga(settings):
    settings.GOOGLE_ANALYTICS_ID = "G-TEST123"


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/", "/signup/", "/privacy/"])
def test_public_pages_ask_before_loading_analytics(client, ga, url):
    html = client.get(url).content.decode()
    assert "data-consent-bar" in html
    assert 'data-consent="granted"' in html and 'data-consent="denied"' in html
    assert "js/consent.js" in html
    # The script tag is only built after consent: either the cookie says so, or Accept fires.
    loader = html[html.index("window.dataLayer") : html.index(LOADER)]
    assert "analytics_consent=granted" in html
    assert "akilent:analytics-consent" in html
    assert "document.head.appendChild" not in loader.split("function loadGA")[0]


@pytest.mark.django_db
def test_the_old_dismiss_only_notice_is_gone(client, ga):
    html = client.get("/signup/").content.decode()
    assert "cookie_notice=1" not in html


@pytest.mark.django_db
def test_the_cookie_policy_lets_you_change_your_choice(client, django_user_model):
    html = client.get("/cookies/").content.decode()
    assert "data-consent-reset" in html and "data-consent-bar" in html
    client.force_login(
        django_user_model.objects.create_user("a", "a@example.com", "pw")
    )
    html = client.get("/cookies/").content.decode()
    assert "data-consent-reset" in html and "data-consent-bar" in html
