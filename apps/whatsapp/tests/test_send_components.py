"""Send-time template components.

Regression cover for the bug where `payload["params"]` (a label -> value dict)
was handed to Meta as `components` (a list), so every template with a variable
failed to send.
"""
import pytest

from apps.accounts.models import Account
from apps.whatsapp.models import MessageTemplate
from apps.whatsapp.send_components import build_send_components


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Co", slug="co")


def _template(account, **kwargs):
    defaults = {
        "account": account, "name": "Payment reminder",
        "whatsapp_template_name": "payment_reminder", "language_code": "en",
        "content": "Hi {{1}}, order {{2}} is unpaid.",
        "variables": ["Customer name", "Order number"],
    }
    return MessageTemplate.objects.create(**{**defaults, **kwargs})


@pytest.mark.django_db
def test_body_parameters_follow_template_variable_order(account):
    template = _template(account)
    components = build_send_components(
        template, {"Order number": "1029", "Customer name": "Ada"}
    )
    assert components == [{
        "type": "body",
        "parameters": [{"type": "text", "text": "Ada"}, {"type": "text", "text": "1029"}],
    }]


@pytest.mark.django_db
def test_missing_value_becomes_empty_string_not_a_gap(account):
    template = _template(account)
    components = build_send_components(template, {"Customer name": "Ada"})
    # Meta rejects a parameter count that doesn't match the template, so a
    # missing value must still occupy its position.
    assert [p["text"] for p in components[0]["parameters"]] == ["Ada", ""]


@pytest.mark.django_db
def test_template_without_variables_sends_no_components(account):
    template = _template(account, content="Thanks for your order!", variables=[])
    assert build_send_components(template, {}) == []


@pytest.mark.django_db
def test_url_button_variable_reuses_its_body_value(account):
    template = _template(account, buttons=[
        {"type": "URL", "text": "Pay now", "url": "https://pay.example.com/{{2}}"},
    ])
    components = build_send_components(
        template, {"Customer name": "Ada", "Order number": "1029"}
    )
    assert components[1] == {
        "type": "button", "sub_type": "url", "index": "0",
        "parameters": [{"type": "text", "text": "1029"}],
    }


@pytest.mark.django_db
def test_static_button_adds_no_parameter(account):
    template = _template(account, buttons=[
        {"type": "PHONE_NUMBER", "text": "Call us", "phone_number": "+15551234567"},
    ])
    components = build_send_components(
        template, {"Customer name": "Ada", "Order number": "1029"}
    )
    assert len(components) == 1  # body only


@pytest.mark.django_db
def test_media_header_template_is_refused_with_a_reason(account):
    template = _template(account, header_format=MessageTemplate.HeaderFormat.IMAGE)
    with pytest.raises(ValueError, match="media header"):
        build_send_components(template, {"Customer name": "Ada"})
