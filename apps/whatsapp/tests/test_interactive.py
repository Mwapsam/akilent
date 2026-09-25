"""WhatsApp reply buttons and lists: parsing what customers tap, and building what we send."""
import time
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from apps.whatsapp import interactive as wa
from apps.whatsapp.models import MessageLog, OutboundMessage, WebhookEventLog, WhatsAppContact
from apps.whatsapp.tasks import _handle_inbound_message, _log_content_for_payload
from apps.whatsapp.tests.test_auto_reply import PHONE, WA_ID, Base

# --- what a tap looks like on the wire -----------------------------------------------------------

BUTTON_REPLY = {"type": "interactive", "interactive": {
    "type": "button_reply", "button_reply": {"id": "prices", "title": "View prices"}}}
LIST_REPLY = {"type": "interactive", "interactive": {
    "type": "list_reply", "list_reply": {"id": "sofa", "title": "Sofa", "description": "3 seater"}}}
TEMPLATE_BUTTON = {"type": "button", "button": {"payload": "yes", "text": "Yes please"}}


@pytest.mark.parametrize("message, expected", [
    (BUTTON_REPLY, {"id": "prices", "title": "View prices", "kind": "button_reply"}),
    (LIST_REPLY, {"id": "sofa", "title": "Sofa", "kind": "list_reply"}),
    (TEMPLATE_BUTTON, {"id": "yes", "title": "Yes please", "kind": "template_button"}),
    ({"type": "text", "text": {"body": "hi"}}, None),
    ({"type": "interactive", "interactive": {"type": "nfm_reply"}}, None),
    ({"type": "image"}, None),
])
def test_extract_reply(message, expected):
    assert wa.extract_reply(message) == expected


# --- building what we send -------------------------------------------------------------------------


def test_buttons_are_built_in_metas_shape_with_ids_from_titles():
    built = wa.build_buttons("What do you need?", [{"title": "View prices"}, {"title": "Book", "id": "book_now"}])
    assert built == {
        "type": "button", "body": {"text": "What do you need?"},
        "action": {"buttons": [
            {"type": "reply", "reply": {"id": "view-prices", "title": "View prices"}},
            {"type": "reply", "reply": {"id": "book_now", "title": "Book"}},
        ]},
    }
    assert wa.option_titles(built) == ["View prices", "Book"]


def test_a_list_is_built_with_one_section_and_optional_descriptions():
    built = wa.build_list("Pick one", "Choose", [{"title": "Sofa", "description": "3 seater"}, {"title": "Bed"}])
    rows = built["action"]["sections"][0]["rows"]
    assert built["type"] == "list" and built["action"]["button"] == "Choose"
    assert rows == [{"id": "sofa", "title": "Sofa", "description": "3 seater"}, {"id": "bed", "title": "Bed"}]
    assert wa.option_titles(built) == ["Sofa", "Bed"]


@pytest.mark.parametrize("call, message", [
    (lambda: wa.build_buttons("Hi", []), "between 1 and 3"),
    (lambda: wa.build_buttons("Hi", [{"title": str(i)} for i in range(4)]), "between 1 and 3"),
    (lambda: wa.build_buttons("Hi", [{"title": "x" * 21}]), "at most 20"),
    (lambda: wa.build_buttons("Hi", [{"title": ""}]), "can't be empty"),
    (lambda: wa.build_buttons("", [{"title": "A"}]), "can't be empty"),
    (lambda: wa.build_buttons("x" * 1025, [{"title": "A"}]), "at most 1024"),
    (lambda: wa.build_buttons("Hi", [{"title": "Yes"}, {"title": "yes"}]), "share the name"),
    (lambda: wa.build_buttons("Hi", [{"title": "!!!"}]), "letters or numbers"),
    (lambda: wa.build_list("Hi", "Go", [{"title": str(i)} for i in range(11)]), "between 1 and 10"),
    (lambda: wa.build_list("Hi", "", [{"title": "A"}]), "can't be empty"),
    (lambda: wa.build_list("Hi", "x" * 21, [{"title": "A"}]), "at most 20"),
    (lambda: wa.build_list("Hi", "Go", [{"title": "x" * 25}]), "at most 24"),
    (lambda: wa.build_list("Hi", "Go", [{"title": "A", "description": "x" * 73}]), "at most 72"),
])
def test_limits_whatsapp_would_reject_are_refused_up_front(call, message):
    with pytest.raises(wa.InteractiveError, match=message):
        call()


# --- the provider -------------------------------------------------------------------------------------


def test_meta_provider_posts_an_interactive_message():
    from apps.whatsapp.providers.meta import MetaCloudAPIProvider

    provider = MetaCloudAPIProvider("token", "PNID")
    interactive = wa.build_buttons("Hi", [{"title": "A"}])
    with patch.object(provider, "_post_message", return_value={"messages": [{"id": "wamid.9"}]}) as post:
        result = provider.send_interactive("+260971234567", interactive)
    assert result.success and result.message_id == "wamid.9"
    assert post.call_args.args[0] == {
        "messaging_product": "whatsapp", "to": "260971234567", "type": "interactive", "interactive": interactive}


# --- an inbound tap, through the real webhook handler ----------------------------------------------------


def interactive_payload(message, msg_id="wamid.tap", text_only=False):
    body = {"from": WA_ID, "id": msg_id, "timestamp": str(int(time.time())), **message}
    return {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "PNID"}, "contacts": [{"profile": {"name": "Tester"}}],
        "messages": [body]}}]}]}


class InboundTapTest(Base):
    def receive(self, message, **kw):
        event = WebhookEventLog.objects.create(
            source="whatsapp", event_type="message", payload=interactive_payload(message, **kw))
        _handle_inbound_message(event)

    def setUp(self):
        super().setUp()
        p = patch("apps.whatsapp.tasks._auto_reply_during_setup")
        p.start()
        self.addCleanup(p.stop)

    def test_a_button_tap_is_stored_as_readable_text_and_keeps_the_choice(self):
        from apps.conversations.models import Message

        self.receive(BUTTON_REPLY)
        log = MessageLog.objects.get(direction="in")
        self.assertEqual((log.content, log.message_type), ("View prices", "text"))
        spine = Message.objects.get(whatsapp_message=log)
        self.assertEqual(spine.body, "View prices")
        self.assertEqual(spine.metadata["reply"], {"id": "prices", "title": "View prices", "kind": "button_reply"})

    def test_a_list_choice_and_a_template_button_are_read_too(self):
        self.receive(LIST_REPLY, msg_id="wamid.l")
        self.receive(TEMPLATE_BUTTON, msg_id="wamid.b")
        self.assertEqual(
            list(MessageLog.objects.filter(direction="in").order_by("id").values_list("content", flat=True)),
            ["Sofa", "Yes please"])

    def test_a_button_labelled_stop_does_not_opt_the_customer_out(self):
        """Opt-out is for typed messages; a menu option that happens to say "Stop" is not one."""
        stop = {"type": "interactive", "interactive": {
            "type": "button_reply", "button_reply": {"id": "stop_updates", "title": "Stop"}}}
        self.receive(stop)
        self.assertNotEqual(WhatsAppContact.objects.get().opt_in_status, "opted_out")


# --- the outbound side -----------------------------------------------------------------------------------


class OutboundInteractiveTest(Base):
    def setUp(self):
        super().setUp()
        self.contact = WhatsAppContact.objects.create(account=self.account, phone_number=PHONE)

    def test_send_interactive_queues_a_message_carrying_the_options(self):
        from apps.whatsapp import api

        interactive = wa.build_buttons("What do you need?", [{"title": "Prices"}, {"title": "Book"}])
        msg = api.send_interactive(self.account, self.contact, interactive)
        self.assertEqual(msg.payload["type"], "interactive")
        self.assertEqual(msg.payload["options"], ["Prices", "Book"])
        self.assertEqual(
            _log_content_for_payload(msg.payload), "What do you need?\n\n[Prices | Book]")

    def test_a_queued_interactive_message_goes_to_the_provider_as_interactive(self):
        from apps.whatsapp import api
        from apps.whatsapp.tasks import _send_outbound

        interactive = wa.build_buttons("Pick", [{"title": "A"}])
        msg = api.send_interactive(self.account, self.contact, interactive)
        provider = MagicMock()
        provider.send_interactive.return_value = MagicMock(success=True, message_id="wamid.x")
        result = _send_outbound(provider, self.contact, msg.payload)
        provider.send_interactive.assert_called_once_with(PHONE, interactive)
        provider.send_text.assert_not_called()
        self.assertEqual(result, {"success": True, "message_id": "wamid.x"})

    def test_interactive_messages_are_refused_outside_the_24_hour_window(self):
        from apps.whatsapp import api
        from apps.whatsapp.tasks import SendNotAuthorized, _authorize_send

        msg = api.send_interactive(self.account, self.contact, wa.build_buttons("Pick", [{"title": "A"}]))
        with self.assertRaises(SendNotAuthorized) as caught:
            _authorize_send(msg)
        self.assertEqual(caught.exception.code, "OUTSIDE_WINDOW_NO_TEMPLATE")
