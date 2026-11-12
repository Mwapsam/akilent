"""A template send reads as words in the inbox, never as Meta's raw template name."""

from apps.conversations.views import _shown_body
from apps.whatsapp.tasks import _log_content_for_payload, humanize_template_name


def test_unknown_template_falls_back_to_a_readable_name():
    payload = {"type": "template", "template_name": "welcome_new_customer"}
    assert _log_content_for_payload(payload) == "Welcome new customer"


def test_humanize_template_name():
    assert humanize_template_name("order_ready") == "Order ready"
    assert humanize_template_name("") == ""


def test_stored_raw_names_are_shown_as_words():
    meta = {"message_type": "template"}
    assert _shown_body("authentication_code_copy_code_button", meta) == (
        "Authentication code copy code button"
    )
    # A rendered body, or any non-template message, is left alone.
    assert _shown_body("Hi Sam, your order is ready.", meta) == (
        "Hi Sam, your order is ready."
    )
    assert _shown_body("order_ready", {"message_type": "text"}) == "order_ready"
