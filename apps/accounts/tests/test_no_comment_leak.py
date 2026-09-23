"""No template commentary may reach the rendered page.

A multi-line {# #} stops being a comment and renders verbatim, which shipped
prose about HTMX and sidebar breakpoints into the <body> of every page. This
renders the real views and asserts that distinctive phrases from the comments
are absent.
"""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership

# Distinctive fragments that only ever appear inside template comments.
COMMENT_FRAGMENTS = [
    "which all descendants inherit",
    "touch tablet cannot hover",
    "off-canvas drawer below lg",
    "Deferred like Alpine",
    "a fixed-width time column",
    "the dashboard's primary question",
    "Their aggregates are deliberately",
]


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    acc = Account.objects.create(
        company_name="Acme",
        selected_services=Account.Services.EMAIL,
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=True,
    )
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/dashboard/", "/dashboard/panels/", "/dashboard/work-queue/"])
def test_no_comment_text_in_rendered_page(client, account, url):
    client.force_login(account.owner)
    body = client.get(url).content.decode()
    leaked = [f for f in COMMENT_FRAGMENTS if f in body]
    assert not leaked, f"Template comment text rendered into {url}: {leaked}"


@pytest.mark.django_db
def test_no_comment_markers_in_rendered_page(client, account):
    client.force_login(account.owner)
    body = client.get("/dashboard/").content.decode()
    for marker in ("{#", "#}", "{% comment %}", "{% endcomment %}"):
        assert marker not in body, f"{marker!r} reached the browser"
