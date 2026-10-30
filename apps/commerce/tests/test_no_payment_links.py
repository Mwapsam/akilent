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


def test_no_action_can_ask_a_customer_for_money():
    assert "request_payment" not in available_actions()
