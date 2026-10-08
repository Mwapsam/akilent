"""Sending photos, files and voice notes from the inbox, on WhatsApp and Instagram."""

import json
import secrets
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations import outbound_media
from apps.conversations.actions import run_action
from apps.conversations.media import media_json
from apps.conversations.models import Conversation, Message
from apps.instagram.providers.base import SendResult as IgSendResult
from apps.instagram.tests.helpers import (
    dm_payload,
    make_account,
    make_instagram_account,
    signed_post,
)
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, OutboundMessage, WhatsAppContact

_MEDIA_ROOT = tempfile.mkdtemp(prefix="akilent-outbound-media-")


def tearDownModule():
    shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)


def _upload(name, content, mime):
    return SimpleUploadedFile(name, content, content_type=mime)


def _fake_ffmpeg(argv, **kwargs):
    """Stand-in for ffmpeg: write the output file the real one would produce."""
    from pathlib import Path

    Path(argv[-1]).write_bytes(b"converted-audio")

    class Done:
        returncode = 0

    return Done()


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class PrepareTest(TestCase):
    def test_photo_is_stored_as_is(self):
        media = outbound_media.prepare(
            _upload("p.png", b"png-bytes", "image/png"), "whatsapp", account_id=1
        )
        self.assertEqual((media.kind, media.mime), ("image", "image/png"))
        self.assertTrue(media.path.startswith("outbound/1/"))

    @patch(
        "apps.conversations.outbound_media.shutil.which", return_value="/usr/bin/ffmpeg"
    )
    @patch("apps.conversations.outbound_media.subprocess.run", side_effect=_fake_ffmpeg)
    def test_chrome_voice_note_becomes_ogg_for_whatsapp(self, run, which):
        media = outbound_media.prepare(
            _upload("voice-note.webm", b"webm", "audio/webm;codecs=opus"),
            "whatsapp",
            account_id=1,
            voice=True,
        )
        self.assertEqual((media.kind, media.mime), ("audio", "audio/ogg"))
        self.assertIn("libopus", run.call_args.args[0])

    @patch(
        "apps.conversations.outbound_media.shutil.which", return_value="/usr/bin/ffmpeg"
    )
    @patch("apps.conversations.outbound_media.subprocess.run", side_effect=_fake_ffmpeg)
    def test_voice_note_becomes_m4a_for_instagram(self, run, which):
        media = outbound_media.prepare(
            _upload("voice-note.ogg", b"ogg", "audio/ogg"),
            "instagram",
            account_id=1,
            voice=True,
        )
        self.assertEqual(media.mime, "audio/mp4")
        self.assertIn("aac", run.call_args.args[0])

    @patch("apps.conversations.outbound_media.shutil.which", return_value=None)
    def test_missing_ffmpeg_is_a_clear_error(self, which):
        with self.assertRaisesMessage(outbound_media.MediaError, "ffmpeg"):
            outbound_media.prepare(
                _upload("v.webm", b"x", "audio/webm"),
                "whatsapp",
                account_id=1,
                voice=True,
            )

    def test_instagram_only_sends_pdf_documents(self):
        with self.assertRaisesMessage(outbound_media.MediaError, "PDF"):
            outbound_media.prepare(
                _upload("a.docx", b"doc", "application/vnd.openxmlformats"),
                "instagram",
                account_id=1,
            )

    def test_too_large_photo_is_refused(self):
        with self.assertRaisesMessage(outbound_media.MediaError, "5 MB"):
            outbound_media.prepare(
                _upload("big.jpg", b"x" * (5 * 1024 * 1024 + 1), "image/jpeg"),
                "whatsapp",
                account_id=1,
            )


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class WhatsAppSendMediaTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(
            company_name="Shop", slug=f"s-{secrets.token_hex(3)}"
        )
        contact = Contact.objects.create(account=self.account, phone="+260970000777")
        self.wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260970000777", contact=contact
        )
        self.wa_conversation = WhatsAppConversation.get_or_open(self.wa_contact)
        self.conversation = Conversation.get_or_create_for_whatsapp(
            self.wa_conversation
        )

    def test_voice_note_is_queued_without_a_caption_and_shows_in_the_thread(self):
        from apps.whatsapp.tasks import _ensure_outbound_log

        media = outbound_media.PreparedMedia(
            path="outbound/1/v.ogg",
            mime="audio/ogg",
            kind="audio",
            filename="voice-note.ogg",
        )
        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            result = run_action(
                "reply_media",
                {"account": self.account},
                conversation=self.conversation,
                media=media,
                caption="ignored for audio",
            )
        outbound = OutboundMessage.objects.get(pk=result["outbound_message_id"])
        self.assertEqual(outbound.payload["type"], "audio")
        self.assertEqual(outbound.payload["caption"], "")
        self.assertEqual(outbound.payload["_conversation_id"], self.wa_conversation.pk)

        log = _ensure_outbound_log(outbound)
        self.assertEqual(log.media_file.name, "outbound/1/v.ogg")
        message = Message.objects.create(
            account=self.account,
            conversation=self.conversation,
            whatsapp_message=log,
            direction="outbound",
            body="",
            timestamp=timezone.now(),
        )
        media_info = media_json(message, self.conversation.public_id)
        self.assertEqual((media_info["kind"], media_info["state"]), ("audio", "ready"))

    def test_provider_omits_caption_for_audio(self):
        from apps.whatsapp.providers.meta import MetaCloudAPIProvider

        provider = MetaCloudAPIProvider.__new__(MetaCloudAPIProvider)
        with patch.object(
            MetaCloudAPIProvider,
            "_post_message",
            return_value={"messages": [{"id": "wamid.1"}]},
            create=True,
        ) as post:
            provider.send_media("+260970000777", "audio", "MID", "hello")
        self.assertEqual(post.call_args.args[0]["audio"], {"id": "MID"})


@override_settings(MEDIA_ROOT=_MEDIA_ROOT, SITE_URL="https://akilent.test")
class InstagramSendMediaTest(TestCase):
    def setUp(self):
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event
        from apps.instagram.views import InstagramWebhookView

        self.account, self.user = make_account()
        self.ig_account = make_instagram_account(self.account)
        with patch("apps.instagram.views.process_instagram_event"):
            InstagramWebhookView.as_view()(
                signed_post(dm_payload(self.ig_account.page_id, "igsid_m1", "hello"))
            )
        process_instagram_event(WebhookEventLog.objects.get().pk)
        self.conversation = Conversation.objects.get(channel="instagram")

    def test_photo_goes_by_signed_link_and_caption_follows(self):
        from apps.instagram.models import InstagramMessage
        from apps.instagram.services.outbound import resolve_media_token

        media = outbound_media.prepare(
            _upload("p.jpg", b"jpg-bytes", "image/jpeg"),
            "instagram",
            account_id=self.account.pk,
        )
        with (
            patch(
                "apps.instagram.providers.meta.MetaInstagramProvider.send_attachment",
                return_value=IgSendResult(
                    success=True, provider_message_id="mid.photo"
                ),
            ) as send_attachment,
            patch(
                "apps.instagram.providers.meta.MetaInstagramProvider.send_message",
                return_value=IgSendResult(success=True, provider_message_id="mid.cap"),
            ) as send_text,
        ):
            run_action(
                "reply_media",
                {"account": self.account},
                conversation=self.conversation,
                media=media,
                caption="Here it is!",
            )
        recipient, kind, url = send_attachment.call_args.args
        self.assertEqual((recipient, kind), ("igsid_m1", "image"))
        self.assertTrue(url.startswith("https://akilent.test/instagram/media/"))
        token = url.rstrip("/").rsplit("/", 1)[-1]
        self.assertEqual(resolve_media_token(token), (media.path, "image/jpeg"))
        send_text.assert_called_once_with("igsid_m1", "Here it is!")

        sent = InstagramMessage.objects.get(message_id="mid.photo")
        self.assertEqual(sent.media_file.name, media.path)
        message = Message.objects.select_related("instagram_message").get(
            instagram_message=sent
        )
        self.assertEqual(
            media_json(message, self.conversation.public_id)["kind"], "image"
        )

    def test_signed_link_serves_only_the_signed_file(self):
        from apps.instagram.services.outbound import signed_media_url
        from apps.instagram.views import instagram_outbound_media

        media = outbound_media.prepare(
            _upload("p.png", b"png!", "image/png"), "instagram", account_id=1
        )
        token = signed_media_url(media.path, media.mime).rstrip("/").rsplit("/", 1)[-1]
        response = instagram_outbound_media(RequestFactory().get("/"), token)
        self.assertEqual(b"".join(response.streaming_content), b"png!")
        with self.assertRaises(Http404):
            instagram_outbound_media(RequestFactory().get("/"), token[:-2] + "xx")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class SendMediaEndpointTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(
            company_name="Shop", slug=f"e-{secrets.token_hex(3)}"
        )
        self.user = User.objects.create_user(f"u{secrets.token_hex(3)}", password="p")
        contact = Contact.objects.create(account=self.account, phone="+260970000888")
        wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260970000888", contact=contact
        )
        wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
        self.conversation = Conversation.get_or_create_for_whatsapp(wa_conversation)

    def _post(self, data):
        from apps.conversations.views import send_media

        request = RequestFactory().post("/", data)
        request.user = self.user
        with patch(
            "apps.conversations.views.get_current_account", return_value=self.account
        ):
            return send_media(request, self.conversation.public_id)

    def test_photo_upload_is_queued(self):
        with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
            response = self._post(
                {"file": _upload("p.jpg", b"jpg", "image/jpeg"), "caption": "New stock"}
            )
        self.assertEqual(response.status_code, 200, response.content)
        outbound = OutboundMessage.objects.get()
        self.assertEqual(
            (outbound.payload["type"], outbound.payload["caption"]),
            ("image", "New stock"),
        )

    def test_missing_file_is_a_readable_error(self):
        response = self._post({})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Choose a file", json.loads(response.content)["error"])

    def test_unsupported_file_is_a_readable_error(self):
        response = self._post({"file": _upload("x.webp", b"w", "image/webp")})
        self.assertEqual(response.status_code, 400)
        self.assertIn("JPG or PNG", json.loads(response.content)["error"])
        self.assertFalse(MessageLog.objects.exists())
