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
    """The landing page's design lives in static/css/landing.css, on the app's tokens."""
    css = (settings.BASE_DIR / "static" / "css" / "landing.css").read_text(
        encoding="utf-8"
    )
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", css)
    assert not re.search(r"\b(rgba?|hsla?)\(", css)
    assert "var(--color-ink-fixed)" in css
    html = _read("accounts/landing.html")
    assert "<style" not in html
    # The browser-chrome colour can't be a var(), so it must equal the navy canvas token.
    app_css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    navy = re.search(r"--color-gray-950:\s*(#[0-9A-Fa-f]{6});", app_css).group(1)
    assert f'name="theme-color" content="{navy}"' in html


def test_fixed_ink_backs_text_on_amber():
    css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    assert "--color-on-amber: var(--color-ink-fixed);" in css
    assert "--color-ink-fixed: #12182B;" in css


def test_templates_fall_back_to_the_product_name():
    for path in TEMPLATES.rglob("*.html"):
        assert "Automator" not in path.read_text(encoding="utf-8"), path


def test_the_code_falls_back_to_the_product_name_too():
    """Gap G4: the site name's default and every Python fallback say "Akilent".

    The one exception is the SES "Source" message tag, a machine label AWS-side rules may
    filter on and that no one sees (apps/email/providers/ses/provider.py).
    """
    from apps.core.models import SiteSettings

    assert SiteSettings._meta.get_field("app_name").default == "Akilent"
    apps_dir = settings.BASE_DIR / "apps"
    found = []
    for path in apps_dir.rglob("*.py"):
        if "migrations" in path.parts or "tests" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "Automator" in line and '"Key": "Source"' not in line:
                found.append(f"{path.relative_to(apps_dir)}:{number}")
    assert not found, found
