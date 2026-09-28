"""Phase 4: inbound media download pipeline.

``download_media`` must fetch bytes via the provider and persist them through
the configured Django storage backend, recording size/mime, and must retire a
row after repeated failures so it stops being re-selected.
"""

import tempfile
from unittest.mock import patch

from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Account
from apps.whatsapp.models import Conversation, MessageLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.providers import WhatsAppProviderError
from apps.whatsapp.tasks import download_media
from apps.whatsapp.types import MediaUrlResult


class _MediaProvider:
    def __init__(self, content=b"\xff\xd8\xffbytes", mime="image/jpeg", exc=None):
        self.content = content
        self.mime = mime
        self.exc = exc

    def get_media_url(self, media_id):
        if self.exc:
            raise self.exc
        return MediaUrlResult(
            url="https://media.example/x",
            media_type=self.mime,
            size_bytes=len(self.content),
        )

    def download_media(self, url):
        return self.content


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DownloadMediaTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Test Co", slug="test-co")
        WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="123456789",
            access_token="tok",
            is_active=True,
        )
        from apps.whatsapp import api as whatsapp_api

        self.contact = whatsapp_api.get_or_create_contact(
            self.account, "+260971234567", "U"
        )
        self.convo = Conversation.get_or_open(self.contact)

    def _make_media_log(self, media_id="media-1", message_id="wamid.M1"):
        return MessageLog.objects.create(
            account=self.account,
            conversation=self.convo,
            contact=self.contact,
            direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.IMAGE,
            media_id=media_id,
            media_mime_type="image/jpeg",
            status=MessageLog.Status.DELIVERED,
            timestamp=timezone.now(),
            message_id=message_id,
        )

    def test_downloads_and_stores_media(self):
        log = self._make_media_log()
        provider = _MediaProvider(content=b"hello-bytes")
        with patch(
            "apps.whatsapp.tasks._get_provider_for_account", return_value=provider
        ):
            download_media()

        log.refresh_from_db()
        self.assertTrue(log.media_file)
        self.assertEqual(log.media_size, len(b"hello-bytes"))
        self.assertTrue(default_storage.exists(log.media_file.name))
        with default_storage.open(log.media_file.name, "rb") as fh:
            self.assertEqual(fh.read(), b"hello-bytes")

    def test_failing_row_is_retired_after_max_attempts(self):
        log = self._make_media_log()
        provider = _MediaProvider(exc=WhatsAppProviderError("boom"))
        with patch(
            "apps.whatsapp.tasks._get_provider_for_account", return_value=provider
        ):
            for _ in range(6):
                download_media()

        log.refresh_from_db()
        self.assertFalse(log.media_file)
        self.assertEqual(log.media_attempts, 5)  # capped; not re-selected past 5
        self.assertIn("boom", log.media_error)

    @override_settings(WHATSAPP_MAX_MEDIA_BYTES=4)
    def test_oversize_media_is_rejected(self):
        log = self._make_media_log()
        provider = _MediaProvider(content=b"way-too-big-payload")
        with patch(
            "apps.whatsapp.tasks._get_provider_for_account", return_value=provider
        ):
            download_media()

        log.refresh_from_db()
        self.assertFalse(log.media_file)
        self.assertEqual(log.media_attempts, 1)
        self.assertIn("exceeds", log.media_error.lower())
