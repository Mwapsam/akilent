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
        tag = "{% nav_active 'email/domains email/mailboxes' %}"
        self.assertEqual(_render("/email/mailboxes/", tag), "nav-link-active")
        self.assertEqual(_render("/contacts/", tag), "")

    def test_is_nav_active_is_boolean(self):
        self.assertEqual(
            _render("/logs/messages/", "{% is_nav_active 'logs' %}"), "True"
        )
        self.assertEqual(
            _render("/contacts/", "{% is_nav_active 'logs' %}"), "False"
        )
