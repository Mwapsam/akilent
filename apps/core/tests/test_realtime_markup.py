"""The page asks htmx for extensions by names htmx can find.

htmx splits ``hx-ext`` on commas and drops spaces, so ``hx-ext="preload sse"`` asked for
one extension called "preloadsse": with live updates switched on, the page had neither live
updates nor hover preloading, and nothing said so.
"""

import re

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from apps.accounts.models import Account, Membership

pytestmark = pytest.mark.django_db
VENDORED = {"preload", "sse"}  # static/js/vendor/htmx-ext-*.js


def _body_extensions(html: str) -> list[str]:
    value = re.search(r"<body[^>]*\shx-ext=\"([^\"]*)\"", html).group(1)
    return [name.replace(" ", "") for name in value.split(",")]


@pytest.mark.parametrize("live", [False, True])
def test_every_extension_named_on_the_page_exists(live):
    user = User.objects.create_user("ext", "ext@example.com", "pass")
    account = Account.objects.create(company_name="Ext Co")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    with override_settings(REALTIME_SSE_ENABLED=live):
        html = client.get("/dashboard/").content.decode()
    names = _body_extensions(html)
    assert set(names) <= VENDORED, names
    assert ("sse" in names) is live and "preload" in names
