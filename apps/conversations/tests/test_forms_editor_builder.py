"""Stage 5 rule builder, Forms editor: one primary action in the header (Publish while the
questions on WhatsApp aren't the current ones), the rest in a menu; questions move by drag
or by named Move buttons; the preview uses the shared message markup."""

from unittest.mock import patch

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.conversations.models import ConversationForm

QUESTIONS = [
    {"key": "name", "label": "Your name?", "field_type": "text", "maps_to": ""}
]
FLOW = ConversationForm.Presentation.WHATSAPP_FLOW


@pytest.fixture
def owner(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _form(account, **kw):
    return ConversationForm.objects.create(
        account=account, name="Intake", questions=QUESTIONS, **kw
    )


def _header(client, form):
    page = client.get(f"/inbox/forms/{form.pk}/").content.decode()
    start = page.index(
        'aria-label="Breadcrumb"'
    )  # the page's own header, not the app bar
    return page[start : page.index("</header>", start)]


def test_a_draft_whatsapp_form_offers_publish_not_two_steps(owner):
    client, account = owner
    header = _header(client, _form(account, presentation=FLOW))
    assert "Publish to WhatsApp" in header
    assert "Make it ready to send" not in header


def test_publishing_a_draft_makes_it_ready_too(owner):
    client, account = owner
    form = _form(account, presentation=FLOW)
    with patch("apps.whatsapp.tasks.publish_conversation_flow"):
        client.post(f"/inbox/forms/{form.pk}/", {"action": "publish_flow"})
    form.refresh_from_db()
    assert form.status == ConversationForm.Status.PUBLISHED


def test_a_live_form_has_no_publish_in_the_header_and_archive_in_the_menu(owner):
    client, account = owner
    form = _form(
        account,
        presentation=FLOW,
        status=ConversationForm.Status.PUBLISHED,
        flow_id="F1",
        flow_status=ConversationForm.FlowStatus.PUBLISHED,
    )
    assert not form.flow_needs_publish
    header = _header(client, form)
    assert "data-form-publish" not in header
    assert "row-menu-item" in header and "Archive" in header and "Delete" in header


def test_changes_waiting_say_publish_changes_once(owner):
    client, account = owner
    form = _form(
        account,
        presentation=FLOW,
        status=ConversationForm.Status.PUBLISHED,
        flow_id="F1",
        flow_status=ConversationForm.FlowStatus.NEEDS_REPUBLISH,
    )
    page = client.get(f"/inbox/forms/{form.pk}/").content.decode()
    assert page.count("Publish changes") == 1  # the header only, not the card as well


def test_a_text_form_draft_is_made_ready(owner):
    client, account = owner
    header = _header(client, _form(account, presentation="text"))
    assert "Make it ready to send" in header and "Publish to WhatsApp" not in header


def test_questions_move_by_drag_or_named_buttons_and_preview_is_shared_markup(owner):
    client, account = owner
    page = client.get(f"/inbox/forms/{_form(account).pk}/").content.decode()
    assert "dragStart(i, $event)" in page and "reorder-handle" in page
    assert "'Move question ' + (i + 1) + ' up'" in page
    assert 'class="wa-preview wa-preview--compact"' in page
