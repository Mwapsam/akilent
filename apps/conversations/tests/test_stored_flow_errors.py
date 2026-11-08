"""Errors stored before Flow errors were translated (Stage 1a) read as plain words too.

The Register form on production still had Meta's raw "Meta API 400 [100]: (#100) The
parameter name is required." from before the fix; owners see the translation, operators
keep the raw text under Details.
"""

import pytest

from apps.conversations.flow_errors import GENERIC, is_raw, readable

RAW = "Meta API 400 [100]: (#100) The parameter name is required."


def test_raw_meta_text_is_translated():
    assert is_raw(RAW)
    text = readable(RAW)
    assert "(#100)" not in text and "Meta API" not in text
    assert text  # one of the owner sentences


def test_translated_text_is_left_alone():
    assert (
        readable("Question 2 needs a field name.") == "Question 2 needs a field name."
    )
    assert readable("") == ""
    assert readable(GENERIC) == GENERIC


@pytest.mark.django_db
def test_the_form_shows_the_translation_and_keeps_the_raw_for_operators():
    from apps.accounts.models import Account
    from apps.conversations.models import ConversationForm

    form = ConversationForm(
        account=Account.objects.create(company_name="Acme"),
        name="Register",
        presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
        status=ConversationForm.Status.PUBLISHED,
        flow_status=ConversationForm.FlowStatus.ERROR,
        flow_error=RAW,
    )
    status = form.display_status
    assert status["key"] == "error" and "(#100)" not in status["reason"]
    assert form.flow_error_raw == RAW
