"""Akilent doesn't take payments for businesses, so nothing may offer to (docs/design/onboarding-spec.md
§11.1). Akilent's own subscription billing is separate and lives in templates/billing/."""

from pathlib import Path

from django.conf import settings

from apps.core.actions import available_actions

# Phrases that would offer, promise or describe collecting money for a business.
FORBIDDEN = (
    "Request payment",
    "request_payment",
    "send a payment link",
    "send them a payment link",
    "link to pay",
    "Payment link created",
    'data-claim="payments"',
)


def _templates():
    root = Path(settings.BASE_DIR) / "templates"
    for path in root.rglob("*.html"):
        if "billing" not in path.relative_to(root).parts:
            yield path


def test_no_template_offers_a_payment_link():
    found = [
        f"{path.relative_to(settings.BASE_DIR)}: {phrase}"
        for path in _templates()
        for phrase in FORBIDDEN
        if phrase in path.read_text(encoding="utf-8")
    ]
    assert not found, "payment-link copy is back:\n" + "\n".join(found)


def test_no_whatsapp_starter_or_field_offers_a_payment_link():
    import json

    from apps.whatsapp.starter_templates import STARTER_CATEGORIES

    starters = json.dumps(STARTER_CATEGORIES).lower()
    assert "payment link" not in starters
    assert "pay.example.com" not in starters
    picker = (Path(settings.BASE_DIR) / "apps" / "whatsapp" / "views.py").read_text(
        encoding="utf-8"
    )
    assert '"label": "Payment link"' not in picker


def test_public_copy_does_not_say_akilent_handles_payments():
    footer = (
        Path(settings.BASE_DIR) / "templates" / "accounts" / "landing" / "_footer.html"
    ).read_text(encoding="utf-8")
    assert "and payments for small businesses" not in footer
    demo = (Path(settings.BASE_DIR) / "docs" / "sales" / "demo.md").read_text(
        encoding="utf-8"
    )
    assert "link to pay" not in demo


def test_no_action_can_ask_a_customer_for_money():
    assert "request_payment" not in available_actions()
