"""UI quality programme, Phase 3: account and security screens.

Holds the parts of the account screens that tell a person what is about to happen and
what did happen, so a later template edit can't quietly drop them. The flows themselves
(sign up, two-factor on, a settings change, all keyboard-only) run in the browser:
tools/qa/accounts_qa.mjs.
"""

import re

from django.conf import settings

TEMPLATES = settings.BASE_DIR / "templates"


def _read(rel: str) -> str:
    return (TEMPLATES / rel).read_text(encoding="utf-8")


def test_changing_a_role_or_revoking_an_invite_asks_first():
    team = _read("accounts/settings_team.html")
    role_form = team[team.index("{% url 'member-role' m.pk %}") :][:500]
    assert "data-confirm=" in role_form and "Make admin" in role_form
    revoke_form = team[team.index("{% url 'invite-revoke' inv.pk %}") :][:300]
    assert "data-confirm=" in revoke_form and "data-confirm-danger" in revoke_form


def test_status_words_use_tone_tokens_not_stock_colours():
    """Stock text-green-600 / text-amber-600 measured about 3.3:1 on white."""
    stock = re.compile(r"\btext-(green|red|amber|yellow|emerald|rose|orange)-\d{3}\b")
    for folder in ("accounts", "auth"):
        for path in (TEMPLATES / folder).glob("*.html"):
            assert not stock.search(path.read_text(encoding="utf-8")), path.name


def test_the_landing_palette_points_at_the_app_tokens():
    style = _read("accounts/landing.html").split("<style>", 1)[1].split("</style>")[0]
    assert not re.search(r"#[0-9A-Fa-f]{6}\b", style)
    assert "var(--color-ink-fixed)" in style


def test_fixed_ink_backs_text_on_amber():
    css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    assert "--color-on-amber: var(--color-ink-fixed);" in css
    assert "--color-ink-fixed: #12182B;" in css


def test_templates_fall_back_to_the_product_name():
    for path in TEMPLATES.rglob("*.html"):
        assert "Automator" not in path.read_text(encoding="utf-8"), path
