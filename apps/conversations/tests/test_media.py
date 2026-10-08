"""Customer media in the inbox: photos, videos, voice notes and files, both channels.

- Instagram attachments are downloaded into our storage (their CDN links expire).
- The inbox shows each message's media with a ready / pending / unavailable state,
  and the live feed updates it once a download finishes.
- Files are only served to the business that owns the conversation, with byte
  ranges so voice notes and videos play in Safari.
"""

import secrets
import shutil
import tempfile
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations.media import media_json
from apps.conversations.models import Conversation, Message
from apps.instagram.tests.helpers import (
    dm_payload,
    make_account,
    make_instagram_account,
    signed_post,
)
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact

_MEDIA_ROOT = tempfile.mkdtemp(prefix="akilent-media-test-")


def tearDownModule():
    shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)


def _voice_note_payload(ig_account, igsid, url="https://cdn.example/voice.mp4"):
    payload = dm_payload(ig_account.page_id, igsid, "")
    message = payload["entry"][0]["messaging"][0]["message"]
    message.pop("text")
    message["attachments"] = [{"type": "audio", "payload": {"url": url}}]
    return payload


def _fake_cdn(content: bytes, mime: str):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.headers = {"Content-Type": mime, "Content-Length": str(len(content))}
    resp.iter_content.return_value = [content]
    resp.raise_for_status.return_value = None
    return resp


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class InstagramVoiceNoteTest(TestCase):
    def setUp(self):
        self.account, self.user = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.igsid = f"igsid_{secrets.token_hex(4)}"

    def _receive(self):
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event
        from apps.instagram.views import InstagramWebhookView

        with patch("apps.instagram.views.process_instagram_event"):
            InstagramWebhookView.as_view()(
                signed_post(_voice_note_payload(self.ig_account, self.igsid))
            )
        with patch("apps.instagram.tasks.download_instagram_media.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                process_instagram_event(WebhookEventLog.objects.get().pk)
        return delay

    def test_voice_note_is_downloaded_and_shown_ready(self):
        from apps.instagram.models import InstagramMessage
        from apps.instagram.tasks import download_instagram_media

        delay = self._receive()
        ig_message = InstagramMessage.objects.get()
        delay.assert_called_once_with(ig_message.pk)

        message = Message.objects.select_related("instagram_message").get()
        public_id = message.conversation.public_id
        self.assertEqual(media_json(message, public_id)["state"], "pending")

        with patch(
            "apps.instagram.tasks.requests.get",
            return_value=_fake_cdn(b"voice-bytes", "audio/mp4"),
        ):
            result = download_instagram_media(ig_message.pk)
        self.assertEqual(result["downloaded"], 1)

        message = Message.objects.select_related("instagram_message").get()
        media = media_json(message, public_id)
        self.assertEqual(media["state"], "ready")
        self.assertEqual(media["kind"], "audio")
        self.assertIn(f"/{message.pk}/media/", media["url"])

    def test_oversized_attachment_is_refused(self):
        from apps.instagram.models import InstagramMessage
        from apps.instagram.tasks import download_instagram_media

        self._receive()
        big = _fake_cdn(b"x", "video/mp4")
        big.headers["Content-Length"] = str(10**9)
        with patch("apps.instagram.tasks.requests.get", return_value=big):
            result = download_instagram_media()
        self.assertEqual(result["failed"], 1)
        self.assertFalse(InstagramMessage.objects.get().media_file)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class MediaViewTest(TestCase):
    """The file view: owner-only, inline for playable types, byte ranges for Safari."""

    def setUp(self):
        self.account = Account.objects.create(
            company_name="Shop", slug=f"s-{secrets.token_hex(3)}"
        )
        self.user = User.objects.create_user(f"u{secrets.token_hex(3)}", password="p")
        contact = Contact.objects.create(account=self.account, phone="+260970000123")
        wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260970000123", contact=contact
        )
        wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
        self.log = MessageLog.objects.create(
            account=self.account,
            conversation=wa_conversation,
            contact=wa_contact,
            message_id="wamid.voice",
            direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.AUDIO,
            content="",
            media_id="MEDIA1",
            media_mime_type="audio/ogg",
            status=MessageLog.Status.DELIVERED,
            timestamp=timezone.now(),
        )
        self.conversation = Conversation.get_or_create_for_whatsapp(wa_conversation)
        self.message = Message.objects.create(
            account=self.account,
            conversation=self.conversation,
            direction=Message.Direction.INBOUND,
            body="",
            timestamp=timezone.now(),
            whatsapp_message=self.log,
        )

    def _get(self, account, headers=None):
        from apps.conversations.views import message_media

        request = RequestFactory().get("/", headers=headers or {})
        request.user = self.user
        with patch(
            "apps.conversations.views.get_current_account", return_value=account
        ):
            return message_media(request, self.conversation.public_id, self.message.pk)

    def test_pending_whatsapp_voice_note_has_no_file_yet(self):
        message = Message.objects.select_related("whatsapp_message").get(
            pk=self.message.pk
        )
        media = media_json(message, self.conversation.public_id)
        self.assertEqual((media["kind"], media["state"]), ("audio", "pending"))
        with self.assertRaises(Http404):
            self._get(self.account)

    def test_downloaded_voice_note_streams_with_ranges(self):
        self.log.media_file.save("voice.ogg", ContentFile(b"0123456789"))
        full = self._get(self.account)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full["Content-Type"], "audio/ogg")
        self.assertEqual(full["Accept-Ranges"], "bytes")
        self.assertNotIn("attachment", full.get("Content-Disposition", ""))

        part = self._get(self.account, headers={"Range": "bytes=2-5"})
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part.content, b"2345")
        self.assertEqual(part["Content-Range"], "bytes 2-5/10")

    def test_another_business_cannot_fetch_it(self):
        self.log.media_file.save("voice.ogg", ContentFile(b"secret"))
        other = Account.objects.create(
            company_name="Other", slug=f"o-{secrets.token_hex(3)}"
        )
        with self.assertRaises(Http404):
            self._get(other)

    def test_feed_reports_media_that_finished_downloading(self):
        from apps.conversations.views import messages_feed

        self.log.media_file.save("voice.ogg", ContentFile(b"abc"))
        request = RequestFactory().get(f"/?after={self.message.pk}")
        request.user = self.user
        with patch(
            "apps.conversations.views.get_current_account", return_value=self.account
        ):
            response = messages_feed(request, self.conversation.public_id)
        import json

        media = json.loads(response.content)["media"][str(self.message.pk)]
        self.assertEqual(media["state"], "ready")
