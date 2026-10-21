"""In-Akilent WhatsApp template creation (R1.5c follow-up amendment)."""

from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import Account
from apps.whatsapp.models import MessageTemplate
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.providers import WhatsAppProviderError
from apps.whatsapp.template_builder import (
    TemplateBuilderError,
    build_meta_payload,
    create_and_submit_template,
    validate_fields,
)


class _FakeProvider:
    def __init__(self, fail=None):
        self.calls = []
        self._fail = fail

    def create_template(self, waba_id, payload):
        if self._fail:
            raise self._fail
        self.calls.append((waba_id, payload))
        return {"id": "123", "status": "PENDING"}


class ValidateFieldsTest(TestCase):
    def test_rejects_bad_name(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="Payment Reminder",
                category="utility",
                language="en",
                body="Hi {{1}}",
                variable_labels=["name"],
                variable_examples=["Ada"],
            )

    def test_rejects_non_sequential_variables(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder",
                category="utility",
                language="en",
                body="Hi {{2}}",
                variable_labels=["name"],
                variable_examples=["Ada"],
            )

    def test_rejects_mismatched_label_count(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder",
                category="utility",
                language="en",
                body="Hi {{1}}, order {{2}}",
                variable_labels=["name"],
                variable_examples=["Ada", "1029"],
            )

    def test_rejects_mismatched_example_count(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder",
                category="utility",
                language="en",
                body="Hi {{1}}, order {{2}}",
                variable_labels=["name", "order"],
                variable_examples=["Ada"],
            )

    def test_rejects_blank_example(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder",
                category="utility",
                language="en",
                body="Hi {{1}}",
                variable_labels=["name"],
                variable_examples=["  "],
            )

    def test_accepts_valid_fields(self):
        validate_fields(
            name="payment_reminder",
            category="utility",
            language="en",
            body="Hi {{1}}, order {{2}} is unpaid.",
            variable_labels=["name", "order"],
            variable_examples=["Ada", "1029"],
        )

    def test_accepts_no_variables(self):
        validate_fields(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome to our store!",
            variable_labels=[],
            variable_examples=[],
        )

    def test_rejects_button_text_too_long(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="utility",
                language="en",
                body="Welcome!",
                variable_labels=[],
                variable_examples=[],
                buttons=[{"text": "x" * 26, "url": "https://example.com"}],
            )

    def test_rejects_button_url_without_scheme(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="utility",
                language="en",
                body="Welcome!",
                variable_labels=[],
                variable_examples=[],
                buttons=[{"text": "Visit", "url": "example.com"}],
            )

    def test_rejects_dynamic_button_url_without_example(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder",
                category="utility",
                language="en",
                body="Hi {{1}}, order {{2}} is unpaid.",
                variable_labels=["name", "order"],
                variable_examples=["Ada", "1029"],
                buttons=[{"text": "Pay now", "url": "https://pay.example.com/{{2}}"}],
            )

    def test_rejects_button_url_referencing_variable_not_in_body(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder",
                category="utility",
                language="en",
                body="Hi {{1}}",
                variable_labels=["name"],
                variable_examples=["Ada"],
                buttons=[
                    {
                        "text": "Pay now",
                        "url": "https://pay.example.com/{{2}}",
                        "example": "https://pay.example.com/1",
                    }
                ],
            )

    def test_rejects_button_url_with_more_than_one_placeholder(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder",
                category="utility",
                language="en",
                body="Hi {{1}}, order {{2}} is unpaid.",
                variable_labels=["name", "order"],
                variable_examples=["Ada", "1029"],
                buttons=[
                    {
                        "text": "Pay now",
                        "url": "https://pay.example.com/{{1}}/{{2}}",
                        "example": "https://pay.example.com/1",
                    }
                ],
            )

    def test_rejects_more_than_one_button(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="utility",
                language="en",
                body="Welcome!",
                variable_labels=[],
                variable_examples=[],
                buttons=[
                    {"text": "One", "url": "https://example.com"},
                    {"text": "Two", "url": "https://example.com"},
                ],
            )

    def test_accepts_static_button(self):
        validate_fields(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome!",
            variable_labels=[],
            variable_examples=[],
            buttons=[{"text": "Learn more", "url": "https://example.com"}],
        )

    def test_accepts_button_sharing_a_body_variable(self):
        validate_fields(
            name="reminder",
            category="utility",
            language="en",
            body="Hi {{1}}, order {{2}} is unpaid.",
            variable_labels=["Customer name", "Order number"],
            variable_examples=["Ada", "1029"],
            buttons=[
                {
                    "text": "Pay now",
                    "url": "https://pay.example.com/{{2}}",
                    "example": "https://pay.example.com/1029",
                }
            ],
        )

    def test_accepts_phone_number_button(self):
        validate_fields(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome!",
            variable_labels=[],
            variable_examples=[],
            buttons=[
                {
                    "type": "phone_number",
                    "text": "Call us",
                    "phone_number": "+15551234567",
                }
            ],
        )

    def test_rejects_phone_number_button_with_bad_format(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="utility",
                language="en",
                body="Welcome!",
                variable_labels=[],
                variable_examples=[],
                buttons=[
                    {
                        "type": "phone_number",
                        "text": "Call us",
                        "phone_number": "555-1234",
                    }
                ],
            )

    def test_rejects_phone_number_button_missing_number(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="utility",
                language="en",
                body="Welcome!",
                variable_labels=[],
                variable_examples=[],
                buttons=[{"type": "phone_number", "text": "Call us"}],
            )

    def test_accepts_voice_call_button(self):
        validate_fields(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome!",
            variable_labels=[],
            variable_examples=[],
            # No phone number: it calls the business's own WhatsApp number.
            buttons=[{"type": "voice_call", "text": "Call on WhatsApp"}],
        )

    def test_accepts_repeated_variable(self):
        validate_fields(
            name="thanks",
            category="utility",
            language="en",
            body="Hi {{1}}, thanks for your order. See you soon, {{1}}!",
            variable_labels=["Customer name"],
            variable_examples=["Ada"],
        )

    def test_rejects_variable_in_text_header(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="utility",
                language="en",
                body="Hi {{1}}, welcome.",
                variable_labels=["name"],
                variable_examples=["Ada"],
                header="Hello {{1}}",
            )

    def test_rejects_button_url_variable_not_at_end(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder",
                category="utility",
                language="en",
                body="Order {{1}} is unpaid.",
                variable_labels=["Order number"],
                variable_examples=["1029"],
                buttons=[
                    {
                        "type": "url",
                        "text": "Pay",
                        "url": "https://pay.example.com/{{1}}/now",
                        "example": "https://pay.example.com/1029/now",
                    }
                ],
            )

    def test_rejects_copy_code_example_over_20_chars(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="marketing",
                language="en",
                body="Welcome!",
                variable_labels=[],
                variable_examples=[],
                buttons=[{"type": "copy_code", "text": "Copy", "example": "X" * 21}],
            )

    def test_accepts_copy_code_button(self):
        validate_fields(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome!",
            variable_labels=[],
            variable_examples=[],
            buttons=[
                {"type": "copy_code", "text": "Copy offer code", "example": "SAVE20"}
            ],
        )

    def test_rejects_copy_code_button_without_example(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="utility",
                language="en",
                body="Welcome!",
                variable_labels=[],
                variable_examples=[],
                buttons=[{"type": "copy_code", "text": "Copy offer code"}],
            )

    def test_rejects_unsupported_button_type(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="utility",
                language="en",
                body="Welcome!",
                variable_labels=[],
                variable_examples=[],
                buttons=[{"type": "share_contact", "text": "Share contact"}],
            )

    def test_rejects_image_header_without_handle(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="welcome",
                category="utility",
                language="en",
                body="Welcome!",
                variable_labels=[],
                variable_examples=[],
                header_format="image",
                header_media_handle="",
            )

    def test_accepts_image_header_with_handle(self):
        validate_fields(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome!",
            variable_labels=[],
            variable_examples=[],
            header_format="image",
            header_media_handle="handle123",
        )


class BuildMetaPayloadTest(TestCase):
    def test_includes_body_and_examples(self):
        payload = build_meta_payload(
            name="payment_reminder",
            category="utility",
            language="en",
            body="Hi {{1}}",
            variable_examples=["Ada"],
        )
        self.assertEqual(payload["name"], "payment_reminder")
        self.assertEqual(payload["category"], "UTILITY")
        body_component = next(c for c in payload["components"] if c["type"] == "BODY")
        self.assertEqual(body_component["example"]["body_text"], [["Ada"]])

    def test_header_and_footer_optional(self):
        payload = build_meta_payload(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome!",
            variable_examples=[],
            header="Hello",
            footer="Thanks",
        )
        types = [c["type"] for c in payload["components"]]
        self.assertEqual(types, ["HEADER", "BODY", "FOOTER"])

    def test_no_buttons_component_when_none_given(self):
        payload = build_meta_payload(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome!",
            variable_examples=[],
        )
        types = [c["type"] for c in payload["components"]]
        self.assertNotIn("BUTTONS", types)

    def test_includes_buttons_component_when_given(self):
        payload = build_meta_payload(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome!",
            variable_examples=[],
            buttons=[
                {"type": "URL", "text": "Learn more", "url": "https://example.com"}
            ],
        )
        buttons_component = next(
            c for c in payload["components"] if c["type"] == "BUTTONS"
        )
        self.assertEqual(
            buttons_component["buttons"],
            [{"type": "URL", "text": "Learn more", "url": "https://example.com"}],
        )

    def test_media_header_uses_handle_and_ignores_text_header(self):
        payload = build_meta_payload(
            name="welcome",
            category="utility",
            language="en",
            body="Welcome!",
            variable_examples=[],
            header="ignored when media is set",
            header_format="image",
            header_media_handle="handle123",
        )
        header_component = next(
            c for c in payload["components"] if c["type"] == "HEADER"
        )
        self.assertEqual(
            header_component,
            {
                "type": "HEADER",
                "format": "IMAGE",
                "example": {"header_handle": ["handle123"]},
            },
        )


class CreateAndSubmitTemplateTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="PNID",
            waba_id="WABA1",
            access_token="tok",
            is_active=True,
        )

    def test_creates_pending_template_after_meta_accepts(self):
        provider = _FakeProvider()
        with patch(
            "apps.whatsapp.template_builder.get_whatsapp_provider",
            return_value=provider,
        ):
            tpl = create_and_submit_template(
                self.account,
                name="payment_reminder",
                category="utility",
                language="en",
                body="Hi {{1}}, order {{2}} is unpaid.",
                variable_labels=["Customer name", "Order number"],
                variable_examples=["Ada", "1029"],
            )
        self.assertEqual(tpl.approval_status, MessageTemplate.ApprovalStatus.PENDING)
        self.assertEqual(tpl.variables, ["Customer name", "Order number"])
        self.assertEqual(len(provider.calls), 1)

    def test_persists_header_footer_buttons_and_examples(self):
        provider = _FakeProvider()
        with patch(
            "apps.whatsapp.template_builder.get_whatsapp_provider",
            return_value=provider,
        ):
            tpl = create_and_submit_template(
                self.account,
                name="payment_reminder",
                category="utility",
                language="en",
                body="Hi {{1}}, order {{2}} is unpaid.",
                variable_labels=["Customer name", "Order number"],
                variable_examples=["Ada", "1029"],
                header="Order update",
                footer="Thanks for your business",
                buttons=[
                    {
                        "text": "Pay now",
                        "url": "https://pay.example.com/{{2}}",
                        "example": "https://pay.example.com/1029",
                    }
                ],
            )
        self.assertEqual(tpl.header, "Order update")
        self.assertEqual(tpl.footer, "Thanks for your business")
        self.assertEqual(tpl.variable_examples, ["Ada", "1029"])
        self.assertEqual(
            tpl.buttons,
            [
                {
                    "type": "URL",
                    "text": "Pay now",
                    "url": "https://pay.example.com/{{2}}",
                    "example": ["https://pay.example.com/1029"],
                }
            ],
        )
        # regression guard: apps.automation.variables.resolve (used by campaign
        # sending) treats `variables` as a plain list of label strings used as
        # mapping keys.
        self.assertEqual(tpl.variables, ["Customer name", "Order number"])

    def test_meta_rejection_raises_and_does_not_create_row(self):
        provider = _FakeProvider(fail=WhatsAppProviderError("bad template"))
        with patch(
            "apps.whatsapp.template_builder.get_whatsapp_provider",
            return_value=provider,
        ):
            with self.assertRaises(TemplateBuilderError):
                create_and_submit_template(
                    self.account,
                    name="payment_reminder",
                    category="utility",
                    language="en",
                    body="Hi {{1}}",
                    variable_labels=["name"],
                    variable_examples=["Ada"],
                )
        self.assertFalse(
            MessageTemplate.objects.filter(
                whatsapp_template_name="payment_reminder"
            ).exists()
        )

    def test_rejects_duplicate_name_and_language(self):
        MessageTemplate.objects.create(
            account=self.account,
            name="payment_reminder",
            whatsapp_template_name="payment_reminder",
            language_code="en",
            content="x",
        )
        with self.assertRaises(TemplateBuilderError):
            create_and_submit_template(
                self.account,
                name="payment_reminder",
                category="utility",
                language="en",
                body="Hi {{1}}",
                variable_labels=["name"],
                variable_examples=["Ada"],
            )

    def test_persists_image_header_media(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.whatsapp.models import MessageTemplateAsset

        asset = MessageTemplateAsset.objects.create(
            account=self.account,
            file=SimpleUploadedFile(
                "logo.png", b"fake-bytes", content_type="image/png"
            ),
            content_type="image/png",
            meta_handle="handle123",
        )
        provider = _FakeProvider()
        with patch(
            "apps.whatsapp.template_builder.get_whatsapp_provider",
            return_value=provider,
        ):
            tpl = create_and_submit_template(
                self.account,
                name="promo",
                category="marketing",
                language="en",
                body="Big sale!",
                variable_labels=[],
                variable_examples=[],
                header_format="image",
                header_media=asset,
            )
        self.assertEqual(tpl.header_format, "image")
        self.assertEqual(tpl.header_media_id, asset.id)
        self.assertEqual(tpl.header, "")
        header_component = next(
            c for c in provider.calls[0][1]["components"] if c["type"] == "HEADER"
        )
        self.assertEqual(header_component["example"]["header_handle"], ["handle123"])

    def test_requires_a_connected_number(self):
        WhatsAppBusinessNumber.objects.all().delete()
        with self.assertRaises(TemplateBuilderError):
            create_and_submit_template(
                self.account,
                name="welcome",
                category="utility",
                language="en",
                body="Hi there",
                variable_labels=[],
                variable_examples=[],
            )

    def _submit_with_button(self, button, body="Hi {{1}}, order {{2}} is unpaid."):
        provider = _FakeProvider()
        with patch(
            "apps.whatsapp.template_builder.get_whatsapp_provider",
            return_value=provider,
        ):
            tpl = create_and_submit_template(
                self.account,
                name="with_button",
                category="marketing",
                language="en",
                body=body,
                variable_labels=["Customer name", "Order number"],
                variable_examples=["Ada", "1029"],
                buttons=[button],
            )
        sent = next(
            c for c in provider.calls[0][1]["components"] if c["type"] == "BUTTONS"
        )["buttons"][0]
        return tpl, sent

    def test_url_button_renumbered_to_1_for_meta_but_stored_as_body_number(self):
        tpl, sent = self._submit_with_button(
            {
                "type": "url",
                "text": "Pay now",
                "url": "https://pay.example.com/{{2}}",
                "example": "https://pay.example.com/1029",
            }
        )
        self.assertEqual(sent["url"], "https://pay.example.com/{{1}}")
        # send_components picks the body value from the stored number.
        self.assertEqual(tpl.buttons[0]["url"], "https://pay.example.com/{{2}}")

    def test_copy_code_button_payload_shape(self):
        _, sent = self._submit_with_button(
            {"type": "copy_code", "text": "Copy offer code", "example": "SAVE20"}
        )
        self.assertEqual(sent, {"type": "COPY_CODE", "example": "SAVE20"})

    def test_voice_call_button_payload_shape(self):
        _, sent = self._submit_with_button({"type": "voice_call", "text": "Call us"})
        self.assertEqual(sent, {"type": "VOICE_CALL", "text": "Call us"})


class MetaCreateTemplateErrorTest(TestCase):
    def test_surfaces_meta_user_message(self):
        from unittest.mock import MagicMock

        from apps.whatsapp.providers.meta import MetaCloudAPIProvider

        provider = MetaCloudAPIProvider(access_token="tok", phone_number_id="PNID")
        response = MagicMock(status_code=400)
        response.json.return_value = {
            "error": {
                "message": "Invalid parameter",
                "code": 100,
                "error_subcode": 2388043,
                "error_user_title": "Message template variables are invalid",
                "error_user_msg": "Variables can't be at the start or end of the template.",
            }
        }
        with patch.object(provider._session, "post", return_value=response):
            with self.assertRaises(WhatsAppProviderError) as ctx:
                provider.create_template("WABA1", {"name": "x"})
        self.assertIn("can't be at the start or end", str(ctx.exception))


class TemplateMediaUploadViewTest(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.account = Account.objects.create(company_name="Co", slug="co")
        self.user = get_user_model().objects.create_user(
            username="owner", email="owner@example.com", password="pw"
        )

    def test_provider_receives_the_full_file(self):
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from django.urls import reverse

        from apps.whatsapp.types import MediaHandleResult

        captured = {}

        class _Provider:
            def upload_template_media(self, content, mime_type, filename="upload"):
                captured["content"] = content
                return MediaHandleResult(handle="h1", success=True)

        self.client.force_login(self.user)
        with (
            tempfile.TemporaryDirectory() as media_root,
            override_settings(MEDIA_ROOT=media_root),
            patch("apps.whatsapp.views.get_current_account", return_value=self.account),
            patch(
                "apps.whatsapp.views.get_whatsapp_provider", return_value=_Provider()
            ),
        ):
            resp = self.client.post(
                reverse("whatsapp-template-media-upload"),
                {
                    "header_format": "image",
                    "file": SimpleUploadedFile(
                        "logo.png", b"real-png-bytes", content_type="image/png"
                    ),
                },
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(captured["content"], b"real-png-bytes")
