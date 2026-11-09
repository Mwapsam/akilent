"""Panels (2026-11-09, spec §20): one surface per module on the tinted canvas, and the
Settings side list as a rail with a highlight bar on the page you're on."""

from django.conf import settings


def _css():
    return (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")


def _rule(css, selector):
    start = css.index(selector + " {")
    return css[start : css.index("}", start)]


def test_the_panel_is_a_hairline_surface_with_16px_titles():
    css = _css()
    panel = _rule(css, "  .panel")
    assert "var(--color-surface)" in panel and "1px solid var(--color-border)" in panel
    assert "text-base font-semibold" in _rule(css, "  .panel-title")
    for part in (
        ".panel-head",
        ".panel-body",
        ".panel-foot",
        ".panel-alert",
        ".panel--danger",
    ):
        assert part in css


def test_the_page_under_panels_is_the_canvas():
    assert "background-color: var(--color-canvas)" in _rule(_css(), "  body")


def test_settings_side_list_is_a_rail_with_a_highlight_bar():
    css = _css()
    assert "border-right: 1px solid var(--color-border)" in css
    active = css[css.index('.settings-nav-link[aria-current="page"]::before') :]
    assert "var(--color-highlight)" in active[: active.index("}")]


def test_the_styleguide_shows_panels(admin_client):
    html = admin_client.get("/manage/styleguide/").content.decode()
    assert 'data-sg="panels"' in html and 'class="panel panel--danger"' in html
