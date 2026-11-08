"""Small fixes after D4–D6 went live (2026-11-08)."""

import pytest
from django.conf import settings
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.conversations.benchmarks import _comparison


def _css():
    return (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")


def test_the_settings_picker_is_hidden_from_md_up_after_the_rule_that_shows_it():
    css = _css()
    shown = css.index(".settings-nav-summary {")
    hidden = css.index(
        "@media (min-width: 768px) { .settings-nav-summary { display: none; } }"
    )
    assert hidden > shown


def test_the_first_reply_time_uses_the_one_duration_format():
    rows = _comparison(
        {"median_first_reply_minutes": 1101}, {"median_first_reply_minutes": 4}
    )
    row = next(r for r in rows if r["label"] == "Median time to first reply")
    assert row["before"] == "18h 21m" and row["after"] == "4m"


@pytest.mark.django_db
def test_security_asks_for_the_current_password_with_one_short_hint(client):
    user = User.objects.create_user("o", "o@example.com", "pw-Sup3r-secret")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    page = client.get("/settings/security/").content.decode()
    assert "Current password" in page and "Old password" not in page
    assert "At least 8 characters, not common or all numbers." in page
    assert "<ul>" not in page[page.index("Change password") : page.index("</form>")]

    page = client.post(
        "/settings/security/",
        {"old_password": "wrong", "new_password1": "x", "new_password2": "x"},
    ).content.decode()
    assert "That isn&#x27;t your current password" in page
