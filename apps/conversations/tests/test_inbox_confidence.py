"""UI quality programme, Phase 1: the inbox says what is actually true.

The behaviour itself runs in the browser (static/js/chat.js, inbox_pane.js) and is
verified there; these tests hold the markup contract it depends on, so a later
template edit can't quietly drop the parts that tell a person what happened.
"""

from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

ROOT = settings.BASE_DIR


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("amara", "amara@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _conversation(account, channel="whatsapp"):
    contact = Contact.objects.create(
        account=account, first_name="Sam", phone="+260971234567"
    )
    conv = Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )
    Message.objects.create(
        account=account,
        conversation=conv,
        direction="inbound",
        body="Price for 3?",
        timestamp=timezone.now() - timedelta(minutes=5),
    )
    return conv


def _pane(client, conv):
    return client.get(f"/inbox/{conv.public_id}/").content.decode()


class TestPaneStyles:
    def test_the_pane_carries_no_inline_stylesheet(self, logged_in):
        """It was re-sent with every conversation opened in the two-pane inbox."""
        client, account = logged_in
        body = _pane(client, _conversation(account))
        assert "<style" not in body
        assert 'class="cv" data-channel="whatsapp"' in body

    def test_the_instagram_palette_follows_the_channel_attribute(self, logged_in):
        client, account = logged_in
        css = (ROOT / "assets" / "app.css").read_text(encoding="utf-8")
        assert '.cv[data-channel="instagram"]' in css
        assert ':root[data-theme="dark"] .cv[data-channel="instagram"]' in css


class TestSendOutcomes:
    def test_a_refused_send_is_explained_beside_the_reply_box(self, logged_in):
        client, account = logged_in
        body = _pane(client, _conversation(account))
        error = body[body.index('class="cv-send-error"') :][:400]
        assert 'role="alert"' in error and 'x-text="sendError"' in error

    def test_an_unconfirmed_send_offers_no_blind_resend(self, logged_in):
        """It may already have reached the customer: check, put back knowingly, or drop."""
        client, account = logged_in
        body = _pane(client, _conversation(account))
        assert "Check now" in body
        assert "Put back in reply box" in body
        # "Edit & resend" is only for a send known to have failed.
        assert 'x-show="open && windowOpen && item.m.body && !item.m.unknown"' in body
        # No delivery ticks on a send that was never confirmed.
        assert "!item.m.unknown\" :class=\"{ 'is-read'" in body

    def test_the_thread_says_when_it_has_stopped_updating(self, logged_in):
        client, account = logged_in
        body = _pane(client, _conversation(account))
        notice = body[body.index('class="cv-conn"') :][:200]
        assert 'role="status"' in notice and "connectionText()" in notice


class TestDetailsDrawer:
    def test_the_drawer_follows_the_dialog_contract(self, logged_in):
        client, account = logged_in
        body = _pane(client, _conversation(account))
        aside = body[body.index('<aside id="chat-info"') :][:600]
        assert ":role=\"infoDrawer ? 'dialog' : null\"" in aside
        assert 'x-trap.noscroll.noautofocus.noreturn="infoDrawer"' in aside
        # Esc belongs to window.akDialog (topmost only), not a window-wide handler.
        assert '@keydown.escape.window="infoOpen = false"' not in body
        backdrop = body[body.index('class="cv-backdrop"') :][:120]
        assert "data-dialog-exempt" in backdrop


class TestInboxList:
    def test_a_failed_refresh_is_said_once_in_place(self, logged_in):
        client, _ = logged_in
        body = client.get("/inbox/").content.decode()
        assert "@ak-refresh-failed=" in body
        assert "The inbox isn't updating right now" in body

    def test_a_conversation_that_fails_to_open_shows_an_error_in_the_pane(
        self, logged_in
    ):
        client, _ = logged_in
        body = client.get("/inbox/").content.decode()
        assert '<div id="conversation-pane-inner" data-error-region' in body
        # Try again goes through inbox_pane.js (latest click wins), not a raw replay.
        assert 'data-error-retry-event="inbox-pane-retry"' in body


def test_a_closed_window_is_one_state_in_the_composer(logged_in):
    """No banner, picker button and placeholder each saying it: the composer itself changes."""
    client, account = logged_in
    body = _pane(client, _conversation(account))
    assert body.count('class="cv-closedwin"') == 1
    assert "Window closed: a reply may not be delivered" not in body
    assert (
        "cv-tpl-cta" not in body
        and 'class="cv-window" x-show="!windowOpen"' not in body
    )
