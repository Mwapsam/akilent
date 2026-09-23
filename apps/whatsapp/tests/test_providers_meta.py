"""Phase 6: MetaCloudAPIProvider — happy path, media upload, error classification."""
import responses
from django.test import TestCase, override_settings

from apps.whatsapp.providers.meta import MetaCloudAPIProvider, _graph_api_base


@override_settings(WHATSAPP_GRAPH_VERSION="v21.0")
class MetaProviderTest(TestCase):
    def setUp(self):
        self.provider = MetaCloudAPIProvider(access_token="tok", phone_number_id="PNID")
        self.messages_url = f"{_graph_api_base()}/PNID/messages"

    @responses.activate
    def test_send_text_success(self):
        responses.add(
            responses.POST, self.messages_url,
            json={"messages": [{"id": "wamid.OUT"}]}, status=200,
        )
        result = self.provider.send_text("+260971234567", "hi")
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "wamid.OUT")
        # '+' stripped from recipient
        self.assertIn('"to": "260971234567"', responses.calls[0].request.body.decode())

    @responses.activate
    def test_rate_limit_error_is_retryable(self):
        responses.add(
            responses.POST, self.messages_url,
            json={"error": {"code": 130429, "message": "Rate limit hit"}}, status=429,
        )
        result = self.provider.send_text("+260971234567", "hi")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "130429")
        self.assertTrue(result.retryable)

    @responses.activate
    def test_reengagement_error_is_terminal(self):
        responses.add(
            responses.POST, self.messages_url,
            json={"error": {"code": 131047, "message": "Re-engagement message"}},
            status=400,
        )
        result = self.provider.send_text("+260971234567", "hi")
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "131047")
        self.assertFalse(result.retryable)

    @responses.activate
    def test_template_error_is_terminal(self):
        responses.add(
            responses.POST, self.messages_url,
            json={"error": {"code": 132001, "message": "Template does not exist"}},
            status=400,
        )
        result = self.provider.send_template("+260971234567", "promo", "en", [])
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "132001")
        self.assertFalse(result.retryable)

    @responses.activate
    def test_upload_media_returns_id(self):
        responses.add(
            responses.POST, f"{_graph_api_base()}/PNID/media",
            json={"id": "media-123"}, status=200,
        )
        result = self.provider.upload_media(b"bytes", "image/png", "x.png")
        self.assertTrue(result.success)
        self.assertEqual(result.media_id, "media-123")

    @responses.activate
    def test_upload_template_media_returns_handle(self):
        provider = MetaCloudAPIProvider(access_token="tok", phone_number_id="PNID", app_id="APPID")
        responses.add(
            responses.POST, f"{_graph_api_base()}/APPID/uploads",
            json={"id": "upload:session123"}, status=200,
        )
        responses.add(
            responses.POST, f"{_graph_api_base()}/upload:session123",
            json={"h": "handle-abc"}, status=200,
        )
        result = provider.upload_template_media(b"bytes", "image/png", "x.png")
        self.assertTrue(result.success)
        self.assertEqual(result.handle, "handle-abc")

    def test_upload_template_media_requires_app_id(self):
        from apps.whatsapp.providers.base import WhatsAppProviderError

        with self.assertRaises(WhatsAppProviderError):
            self.provider.upload_template_media(b"bytes", "image/png", "x.png")
