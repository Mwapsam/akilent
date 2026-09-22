"""In-Akilent WhatsApp template creation (R1.5c follow-up amendment)."""
from unittest.mock import patch

import pytest
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
                name="Payment Reminder", category="utility", language="en",
                body="Hi {{1}}", variable_labels=["name"],
            )

    def test_rejects_non_sequential_variables(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder", category="utility", language="en",
                body="Hi {{2}}", variable_labels=["name"],
            )

    def test_rejects_mismatched_label_count(self):
        with self.assertRaises(TemplateBuilderError):
            validate_fields(
                name="reminder", category="utility", language="en",
                body="Hi {{1}}, order {{2}}", variable_labels=["name"],
            )

    def test_accepts_valid_fields(self):
        validate_fields(
            name="payment_reminder", category="utility", language="en",
            body="Hi {{1}}, order {{2}} is unpaid.", variable_labels=["name", "order"],
        )

    def test_accepts_no_variables(self):
        validate_fields(
            name="welcome", category="utility", language="en",
            body="Welcome to our store!", variable_labels=[],
        )


class BuildMetaPayloadTest(TestCase):
    def test_includes_body_and_examples(self):
        payload = build_meta_payload(
            name="payment_reminder", category="utility", language="en",
            body="Hi {{1}}", variable_examples=["Ada"],
        )
        self.assertEqual(payload["name"], "payment_reminder")
        self.assertEqual(payload["category"], "UTILITY")
        body_component = next(c for c in payload["components"] if c["type"] == "BODY")
        self.assertEqual(body_component["example"]["body_text"], [["Ada"]])

    def test_header_and_footer_optional(self):
        payload = build_meta_payload(
            name="welcome", category="utility", language="en", body="Welcome!",
            variable_examples=[], header="Hello", footer="Thanks",
        )
        types = [c["type"] for c in payload["components"]]
        self.assertEqual(types, ["HEADER", "BODY", "FOOTER"])


class CreateAndSubmitTemplateTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        WhatsAppBusinessNumber.objects.create(
            account=self.account, phone_number_id="PNID",
            waba_id="WABA1", access_token="tok", is_active=True,
        )

    def test_creates_pending_template_after_meta_accepts(self):
        provider = _FakeProvider()
        with patch("apps.whatsapp.template_builder.get_whatsapp_provider", return_value=provider):
            tpl = create_and_submit_template(
                self.account, name="payment_reminder", category="utility", language="en",
                body="Hi {{1}}, order {{2}} is unpaid.",
                variable_labels=["Customer name", "Order number"],
                variable_examples=["Ada", "1029"],
            )
        self.assertEqual(tpl.approval_status, MessageTemplate.ApprovalStatus.PENDING)
        self.assertEqual(tpl.variables, ["Customer name", "Order number"])
        self.assertEqual(len(provider.calls), 1)

    def test_meta_rejection_raises_and_does_not_create_row(self):
        provider = _FakeProvider(fail=WhatsAppProviderError("bad template"))
        with patch("apps.whatsapp.template_builder.get_whatsapp_provider", return_value=provider):
            with self.assertRaises(TemplateBuilderError):
                create_and_submit_template(
                    self.account, name="payment_reminder", category="utility", language="en",
                    body="Hi {{1}}", variable_labels=["name"], variable_examples=["Ada"],
                )
        self.assertFalse(MessageTemplate.objects.filter(whatsapp_template_name="payment_reminder").exists())

    def test_rejects_duplicate_name_and_language(self):
        MessageTemplate.objects.create(
            account=self.account, name="payment_reminder", whatsapp_template_name="payment_reminder",
            language_code="en", content="x",
        )
        with self.assertRaises(TemplateBuilderError):
            create_and_submit_template(
                self.account, name="payment_reminder", category="utility", language="en",
                body="Hi {{1}}", variable_labels=["name"], variable_examples=["Ada"],
            )

    def test_requires_a_connected_number(self):
        WhatsAppBusinessNumber.objects.all().delete()
        with self.assertRaises(TemplateBuilderError):
            create_and_submit_template(
                self.account, name="welcome", category="utility", language="en",
                body="Hi there", variable_labels=[], variable_examples=[],
            )
