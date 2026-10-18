"""Meta's "failed" status webhooks: the error lands on the message and its consumers."""

import pytest
from django.utils import timezone

from apps.whatsapp import api as whatsapp_api
from apps.whatsapp.models import OutboundMessage, WhatsAppContact
from apps.whatsapp.tasks import StatusBeforeSend, _process_status_update
from apps.whatsapp.tests.test_outbound import _FakeProvider, _OutboundBase

_VALUE = {"metadata": {"phone_number_id": "123456789"}}


def _failed(message_id, code=131026, title="Message undeliverable"):
    return {
        "id": message_id,
        "status": "failed",
        "timestamp": str(int(timezone.now().timestamp())),
        "errors": [
            {
                "code": code,
                "title": title,
                "error_data": {"details": "Receiver is incapable"},
            }
        ],
    }


class FailedStatusTest(_OutboundBase):
    def _sent(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        self._drain_with(_FakeProvider())
        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.SENT)
        return msg

    def test_failed_status_marks_the_message_failed_with_its_code(self):
        msg = self._sent()
        _process_status_update(_VALUE, _failed("wamid.OUT1"))
        msg.refresh_from_db()
        self.assertEqual(msg.status, OutboundMessage.Status.FAILED)
        self.assertEqual(msg.error_code, "131026")
        self.assertIn("Message undeliverable", msg.last_error)

    def test_failed_status_reaches_the_inbox_with_a_reason(self):
        from apps.conversations.models import Message

        self._sent()
        _process_status_update(_VALUE, _failed("wamid.OUT1"))
        spine = Message.objects.get(direction="outbound")
        self.assertEqual(spine.metadata.get("error_code"), "131026")

    def test_replayed_failed_status_notifies_once(self):
        from unittest.mock import patch

        self._sent()
        with patch("apps.whatsapp.tasks._notify_terminal_failure") as notify:
            _process_status_update(_VALUE, _failed("wamid.OUT1"))
            _process_status_update(_VALUE, _failed("wamid.OUT1"))
        self.assertEqual(notify.call_count, 1)

    def test_marketing_stopped_withdraws_marketing_consent_only(self):
        self.contact.record_opt_in("test")
        self._sent()
        _process_status_update(_VALUE, _failed("wamid.OUT1", code=131050))
        self.contact.refresh_from_db()
        self.assertEqual(
            self.contact.opt_in_status, WhatsAppContact.OptInStatus.UNKNOWN
        )

    def test_status_before_the_send_is_recorded_is_retried(self):
        msg = whatsapp_api.send_message(self.account, self.contact, "hi")
        OutboundMessage.objects.filter(pk=msg.pk).update(
            status=OutboundMessage.Status.SENDING
        )
        with pytest.raises(StatusBeforeSend):
            _process_status_update(_VALUE, _failed("wamid.UNKNOWN"))

    def test_status_for_an_unknown_message_is_ignored_when_nothing_is_in_flight(self):
        _process_status_update(_VALUE, _failed("wamid.UNKNOWN"))
