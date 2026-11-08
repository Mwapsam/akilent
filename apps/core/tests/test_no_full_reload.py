"""Pages opened from inside the app swap in place, never reload the whole page.

ShellMiddleware turns a boosted navigation into a full page load (HX-Redirect) when the
page carries a <script> not marked ``data-shell-safe``. Each page here must swap: its
scripts are safe to run again after a swap (guarded, idempotent) and say so.
"""

import re

import pytest
from django.contrib.auth.models import User
from django.test import override_settings

from apps.accounts.models import Account, Membership

BOOSTED = {
    "HTTP_HX_REQUEST": "true",
    "HTTP_HX_BOOSTED": "true",
    "HTTP_X_AKILENT_SHELL": "app",
}


def _tab_pages() -> list[str]:
    """Every section tab (the sidebar and tab bar link to these): a page with a script
    that isn't shell-safe also turns its own tab links into full loads (app.js
    markUnsafeMain), which is how /build/ reloaded when opened from /automations/."""
    from apps.core.sections import SECTIONS

    return [t.href for s in SECTIONS for t in s.tabs if t.href.startswith("/")]


PAGES = [
    "/automations/",
    "/build/",
    "/build/?step=2",
    "/build/?step=3",
    "/email/templates/",
    "/email/templates/?channel=whatsapp",
    "/inbox/comments/",
    "/inbox/followups/",
    "/automations/why-not/",
    "/inbox/saved-replies/",
    "/inbox/forms/",
]
PAGES += [p for p in _tab_pages() if p not in PAGES]


@pytest.fixture
def owner_client(client, db):
    from apps.billing.models import AccountFeatureOverride

    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    account = Account.objects.create(
        company_name="Acme",
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=True,
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    for key in (
        "automations",
        "whatsapp",
        "instagram",
        "facebook",
        "ai_builder",
        "email",
        "campaigns",
        "insights",
        "content_studio",
        "sales",
    ):
        AccountFeatureOverride.objects.create(account=account, key=key, grant=True)
    client.force_login(user)
    return client


def _unsafe_scripts(html: str) -> list[str]:
    return [
        tag
        for tag in re.findall(r"<script\b[^>]*>", html, re.IGNORECASE)
        if 'type="application/json"' not in tag and "data-shell-safe" not in tag
    ]


@pytest.mark.parametrize("url", PAGES)
@override_settings(WHATSAPP_ENABLED=True)
def test_page_swaps_in_place(owner_client, url):
    from unittest.mock import patch

    from apps.core import htmx

    seen: list[bytes] = []
    real = htmx._needs_full_load

    def spy(content):
        seen.append(content)
        return real(content)

    with patch.object(htmx, "_needs_full_load", spy):
        resp = owner_client.get(url, **BOOSTED)
    if resp.status_code in (301, 302):
        pytest.skip(f"{url} redirects for this account")
    unsafe = _unsafe_scripts(seen[-1].decode()) if seen else []
    assert "HX-Redirect" not in resp, unsafe
