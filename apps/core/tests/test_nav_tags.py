"""Tests for the primary-nav template tags (apps/core/templatetags/nav.py)."""

from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase


def _render(path: str, tag: str) -> str:
    request = RequestFactory().get(path)
    tpl = Template("{% load nav %}" + tag)
    return tpl.render(Context({"request": request})).strip()


class NavActiveTests(SimpleTestCase):
    def test_prefix_match_on_section(self):
        self.assertEqual(
            _render("/email/campaigns/42/", "{% nav_active 'email/campaigns' %}"),
            "nav-link-active",
        )

    def test_no_match_for_sibling_section(self):
        self.assertEqual(
            _render("/email/templates/", "{% nav_active 'email/campaigns' %}"),
            "",
        )

    def test_exact_flag_rejects_subpaths(self):
        self.assertEqual(
            _render("/dashboard/reports/", "{% nav_active 'dashboard' exact=True %}"),
            "",
        )
        self.assertEqual(
            _render("/dashboard/", "{% nav_active 'dashboard' exact=True %}"),
            "nav-link-active",
        )

    def test_url_name_is_reversed(self):
        # 'dashboard' has no slash → treated as a URL name and reversed to /dashboard/
        self.assertEqual(
            _render("/dashboard/", "{% nav_active 'dashboard' %}"),
            "nav-link-active",
        )

    def test_space_separated_targets(self):
        tag = "{% nav_active 'email/domains email/webhooks' %}"
        self.assertEqual(_render("/email/webhooks/", tag), "nav-link-active")
        self.assertEqual(_render("/contacts/", tag), "")

    def test_is_nav_active_is_boolean(self):
        self.assertEqual(
            _render("/logs/messages/", "{% is_nav_active 'logs' %}"), "True"
        )
        self.assertEqual(_render("/contacts/", "{% is_nav_active 'logs' %}"), "False")

    def test_exclude_carves_out_a_subsection(self):
        tag = "{% nav_active 'inbox' exclude='inbox/followups inbox/forms' %}"
        self.assertEqual(_render("/inbox/", tag), "nav-link-active")
        self.assertEqual(_render("/inbox/conv_abc/", tag), "nav-link-active")
        self.assertEqual(_render("/inbox/followups/", tag), "")
        self.assertEqual(_render("/inbox/forms/42/", tag), "")


class PrimaryNavCurrentLinkTests(SimpleTestCase):
    """The rendered sidebar marks exactly one link current on every section.

    Two links marked aria-current="page" tell a screen reader and a sighted user
    two different answers to "where am I?" - Inbox used to light up on the
    Follow-ups and Forms pages too.
    """

    # The sidebar's main item for each page (apps/core/sections.py): Follow-ups is an
    # Inbox tab, Forms an Automations tab, Insights a Home tab, the Activity log Settings.
    ROUTES = {
        "/inbox/": "/inbox/",
        "/inbox/conv_abc/": "/inbox/",
        "/inbox/followups/": "/inbox/",
        "/inbox/forms/": "/automations/",
        "/inbox/forms/7/": "/automations/",
        "/dashboard/": "/dashboard/",
        "/contacts/12/": "/contacts/",
        "/publishing/calendar/": "/publishing/",
        "/insights/": "/dashboard/",
        "/logs/messages/": "/settings/",
    }

    def _current_links(self, path):
        import re

        request = RequestFactory().get(path)
        html = Template('{% include "components/_nav.html" %}').render(
            Context(
                {
                    "request": request,
                    "tools_off": [],
                    "locked_features": [],
                    "CAMPAIGNS_ENABLED": True,
                }
            )
        )
        return re.findall(r'<a href="([^"]+)"[^>]*aria-current="page"', html, re.S)

    def test_exactly_one_current_link_per_route(self):
        for path, expected in self.ROUTES.items():
            with self.subTest(path=path):
                self.assertEqual(self._current_links(path), [expected])
