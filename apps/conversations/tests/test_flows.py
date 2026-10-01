"""WhatsApp Flows feature -- dedicated test suite.

Covers:
  - Provider: create_flow / publish_flow (success + 4xx error) and the
    intentional update_flow_json placeholder raise.
  - extract_reply: valid and malformed nfm_reply payloads.
  - forms.complete_from_flow: happy path, unmatched token, idempotency.
  - forms._start_flow / start_form (flow branch): FormError when unpublished,
    correct interactive payload, flow_token persisted.
  - _enroll_workflows: nfm_reply routes to complete_from_flow and doesn't
    fall through to resume_on_reply / enroll_for_trigger.
  - ConversationForm.note_questions_changed: flips PUBLISHED->NEEDS_REPUBLISH
    on a real content change, ignores non-flow presentation, ignores a change
    that produces the same hash.
  - Settings-view actions: set_presentation and publish_flow POSTs.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import forms as conversation_forms
from apps.conversations.actions import run_action
from apps.conversations.models import (
    Conversation,
    ConversationForm,
    Event,
    FormResponse,
)
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact
from apps.whatsapp.providers.base import WhatsAppProviderError
from apps.whatsapp.providers.meta import MetaCloudAPIProvider

QUESTIONS = [
    {
        "key": "name",
        "label": "What's your name?",
        "field_type": "text",
        "maps_to": "contact.first_name",
    },
    {
        "key": "email",
        "label": "What's your email?",
        "field_type": "email",
        "maps_to": "",
    },
]


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Flow Test Co")


@pytest.fixture
def published_form(account):
    return ConversationForm.objects.create(
        account=account,
        name="Intake",
        status=ConversationForm.Status.PUBLISHED,
        presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
        flow_id="flow_abc123",
        flow_status=ConversationForm.FlowStatus.PUBLISHED,
        flow_json_hash="deadbeef" * 5,
        questions=QUESTIONS,
    )


@pytest.fixture
def wa_setup(account):
    """Returns (contact, wa_contact, wa_convo, generic_convo)."""
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number=contact.phone, contact=contact
    )
    wa_convo = WhatsAppConversation.get_or_open(wa_contact)
    generic = Conversation.get_or_create_for_whatsapp(wa_convo)
    return contact, wa_contact, wa_convo, generic


def _reply_stub(monkeypatch):
    """No-op for the 'reply' action; returns the list of sent texts."""
    sent = []

    def fake_run_action(name, context, **kwargs):
        if name == "reply":
            sent.append(kwargs.get("body", ""))
            return {"outbound_message_id": None}
        return run_action(name, context, **kwargs)

    monkeypatch.setattr("apps.conversations.actions.run_action", fake_run_action)
    return sent


def _mock_response(status_code, body):
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = body
    return m


# ---------------------------------------------------------------------------
# Provider tests (mocked _session.post)
# ---------------------------------------------------------------------------


class TestMetaProviderFlowMethods:
    def _provider(self):
        return MetaCloudAPIProvider("fake-token", "PNID")

    def test_create_flow_success(self):
        provider = self._provider()
        with patch.object(
            provider._session,
            "post",
            return_value=_mock_response(200, {"id": "flow_new_999"}),
        ):
            result = provider.create_flow("WABA123", "My Form", ["OTHER"])
        assert result == {"id": "flow_new_999"}

    def test_create_flow_4xx_raises_provider_error(self):
        provider = self._provider()
        error_body = {
            "error": {"code": 100, "message": "Invalid parameter", "fbtrace_id": "x"}
        }
        with patch.object(
            provider._session, "post", return_value=_mock_response(400, error_body)
        ):
            with pytest.raises(WhatsAppProviderError, match="Meta API 400"):
                provider.create_flow("WABA123", "Bad", ["OTHER"])

    def test_update_flow_json_raises_intentional_placeholder(self):
        """update_flow_json must raise until Meta's asset-upload endpoint is confirmed."""
        provider = self._provider()
        with pytest.raises(WhatsAppProviderError, match="not yet confirmed"):
            provider.update_flow_json("flow_x", {"version": "3.0"})

    def test_publish_flow_success(self):
        provider = self._provider()
        with patch.object(
            provider._session,
            "post",
            return_value=_mock_response(200, {"success": True}),
        ):
            result = provider.publish_flow("flow_x")
        assert result.get("success") is True

    def test_publish_flow_4xx_raises_provider_error(self):
        provider = self._provider()
        error_body = {
            "error": {"code": 200, "message": "Permission denied", "fbtrace_id": "y"}
        }
        with patch.object(
            provider._session, "post", return_value=_mock_response(403, error_body)
        ):
            with pytest.raises(WhatsAppProviderError, match="Meta API 403"):
                provider.publish_flow("flow_x")


# ---------------------------------------------------------------------------
# extract_reply: nfm_reply payloads
# ---------------------------------------------------------------------------


class TestExtractReplyNfmReply:
    def test_valid_nfm_reply_with_response_json(self):
        from apps.whatsapp import interactive as wa

        fields = {"name": "Alice", "email": "alice@example.com"}
        message = {
            "type": "interactive",
            "interactive": {
                "type": "nfm_reply",
                "nfm_reply": {
                    "response_json": json.dumps({"flow_token": "tok123", **fields}),
                    "body": "Sent",
                    "name": "flow",
                },
            },
        }
        reply = wa.extract_reply(message)
        assert reply is not None
        assert reply["kind"] == "nfm_reply"
        assert reply["flow_token"] == "tok123"
        assert reply["fields"] == fields

    def test_nfm_reply_with_unparseable_response_json_returns_empty_fields(self):
        from apps.whatsapp import interactive as wa

        message = {
            "type": "interactive",
            "interactive": {
                "type": "nfm_reply",
                "nfm_reply": {"response_json": "{{not json}}", "body": "Sent"},
            },
        }
        reply = wa.extract_reply(message)
        assert reply is not None
        assert reply["kind"] == "nfm_reply"
        assert reply["fields"] == {}
        assert reply["flow_token"] == ""

    def test_nfm_reply_with_non_dict_response_json_returns_empty_fields(self):
        from apps.whatsapp import interactive as wa

        message = {
            "type": "interactive",
            "interactive": {
                "type": "nfm_reply",
                "nfm_reply": {"response_json": json.dumps(["a", "b"]), "body": "Sent"},
            },
        }
        reply = wa.extract_reply(message)
        assert reply is not None
        assert reply["kind"] == "nfm_reply"
        assert reply["fields"] == {}

    def test_nfm_reply_without_nfm_reply_key_returns_empty_token_and_fields(self):
        from apps.whatsapp import interactive as wa

        reply = wa.extract_reply(
            {"type": "interactive", "interactive": {"type": "nfm_reply"}}
        )
        assert reply == {
            "id": "",
            "title": "",
            "kind": "nfm_reply",
            "flow_token": "",
            "fields": {},
        }


# ---------------------------------------------------------------------------
# forms._start_flow / start_form (flow branch)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestStartFlow:
    def test_start_form_raises_form_error_when_not_published(self, account, wa_setup):
        _, _, _, generic = wa_setup
        unpublished = ConversationForm.objects.create(
            account=account,
            name="Draft",
            status=ConversationForm.Status.PUBLISHED,
            presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
            flow_status=ConversationForm.FlowStatus.NOT_CREATED,
            questions=QUESTIONS,
        )
        with pytest.raises(conversation_forms.FormError, match="hasn't been published"):
            conversation_forms.start_form(generic, unpublished)

    def test_start_form_raises_form_error_when_flow_id_missing(self, account, wa_setup):
        _, _, _, generic = wa_setup
        form = ConversationForm.objects.create(
            account=account,
            name="NoId",
            status=ConversationForm.Status.PUBLISHED,
            presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
            flow_status=ConversationForm.FlowStatus.PUBLISHED,
            flow_id="",
            questions=QUESTIONS,
        )
        with pytest.raises(conversation_forms.FormError):
            conversation_forms.start_form(generic, form)

    def test_start_flow_sends_interactive_with_correct_shape(
        self, monkeypatch, account, published_form, wa_setup
    ):
        _, _, _, generic = wa_setup
        sent_interactives = []

        monkeypatch.setattr(
            "apps.whatsapp.api.send_interactive",
            lambda acct, contact, interactive, **kw: (
                sent_interactives.append(interactive) or MagicMock()
            ),
        )

        response = conversation_forms.start_form(generic, published_form)

        assert len(sent_interactives) == 1
        interactive = sent_interactives[0]
        assert interactive["type"] == "flow"
        params = interactive["action"]["parameters"]
        assert params["flow_id"] == "flow_abc123"
        assert params["flow_action"] == "navigate"
        assert params["flow_action_payload"] == {"screen": "FORM"}

        # flow_token must be persisted on the response row
        response.refresh_from_db()
        assert response.flow_token != ""
        assert params["flow_token"] == response.flow_token


# ---------------------------------------------------------------------------
# forms.complete_from_flow
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCompleteFromFlow:
    def _make_in_progress(self, account, form, generic, token="tok_abc"):
        return FormResponse.objects.create(
            account=account,
            form=form,
            conversation=generic,
            contact=generic.contact,
            flow_token=token,
        )

    def test_happy_path_applies_fields_and_emits_event(
        self, monkeypatch, account, published_form, wa_setup
    ):
        _, _, _, generic = wa_setup
        _reply_stub(monkeypatch)
        self._make_in_progress(account, published_form, generic, token="tok_ok")

        result = conversation_forms.complete_from_flow(
            generic,
            "tok_ok",
            {"name": "Chanda", "email": "chanda@example.com"},
        )

        assert result is True
        response = FormResponse.objects.get(conversation=generic)
        assert response.status == FormResponse.Status.COMPLETED
        assert response.answers == {"name": "Chanda", "email": "chanda@example.com"}
        assert Event.objects.filter(type="conversation.form_completed").exists()

        generic.contact.refresh_from_db()
        assert generic.contact.first_name == "Chanda"

    def test_unmatched_token_returns_false(self, account, published_form, wa_setup):
        _, _, _, generic = wa_setup
        self._make_in_progress(account, published_form, generic, token="tok_real")

        result = conversation_forms.complete_from_flow(
            generic, "tok_wrong", {"name": "X"}
        )
        assert result is False
        assert (
            FormResponse.objects.get(conversation=generic).status
            == FormResponse.Status.IN_PROGRESS
        )

    def test_blank_token_returns_false(self, account, published_form, wa_setup):
        _, _, _, generic = wa_setup
        result = conversation_forms.complete_from_flow(generic, "", {"name": "X"})
        assert result is False

    def test_second_call_after_completion_returns_false_without_firing_event_again(
        self, monkeypatch, account, published_form, wa_setup
    ):
        _, _, _, generic = wa_setup
        _reply_stub(monkeypatch)
        self._make_in_progress(account, published_form, generic, token="tok_idem")

        conversation_forms.complete_from_flow(generic, "tok_idem", {"name": "Alice"})
        event_count = Event.objects.filter(type="conversation.form_completed").count()

        # Second call: response is now COMPLETED, token lookup returns nothing
        result = conversation_forms.complete_from_flow(
            generic, "tok_idem", {"name": "Alice"}
        )
        assert result is False
        assert (
            Event.objects.filter(type="conversation.form_completed").count()
            == event_count
        )

    def test_invalid_field_value_is_skipped_not_stored(
        self, monkeypatch, account, published_form, wa_setup
    ):
        _, _, _, generic = wa_setup
        _reply_stub(monkeypatch)
        self._make_in_progress(account, published_form, generic, token="tok_skip")

        conversation_forms.complete_from_flow(
            generic,
            "tok_skip",
            {"name": "Chanda", "email": "not-an-email"},
        )

        response = FormResponse.objects.get(conversation=generic)
        assert "name" in response.answers
        assert "email" not in response.answers


# ---------------------------------------------------------------------------
# _enroll_workflows: nfm_reply routes to complete_from_flow
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEnrollWorkflowsFlowBranch:
    def test_nfm_reply_is_consumed_by_complete_from_flow_not_record_answer(
        self, monkeypatch, account, published_form, wa_setup
    ):
        _, wa_contact, wa_convo, generic = wa_setup

        _reply_stub(monkeypatch)

        # Start a flow-mode form to put a FormResponse in progress with a known token
        monkeypatch.setattr(
            "apps.whatsapp.api.send_interactive",
            lambda acct, contact, interactive, **kw: MagicMock(),
        )
        response = conversation_forms.start_form(generic, published_form)
        token = response.flow_token
        assert token

        # Build the inbound nfm_reply wrapped in the full webhook envelope so
        # reply_for_log (which walks entry[].changes[].value.messages[]) can
        # extract it.
        nfm_payload = {
            "flow_token": token,
            "name": "Chanda",
            "email": "chanda@example.com",
        }
        msg_body = {
            "id": "wamid.NFM1",
            "type": "interactive",
            "interactive": {
                "type": "nfm_reply",
                "nfm_reply": {
                    "response_json": json.dumps(nfm_payload),
                    "body": "",
                    "name": "flow",
                },
            },
        }
        raw = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": "PNID"},
                                "contacts": [{"profile": {"name": "Chanda"}}],
                                "messages": [msg_body],
                            },
                        }
                    ]
                }
            ]
        }
        log = MessageLog.objects.create(
            account=account,
            conversation=wa_convo,
            contact=wa_contact,
            message_id="wamid.NFM1",
            direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.TEXT,
            content="",
            status=MessageLog.Status.DELIVERED,
            timestamp=timezone.now(),
            raw_payload=raw,
        )

        record_answer_calls = []
        real_record_answer = conversation_forms.record_answer

        def spy_record_answer(conv, text):
            record_answer_calls.append(text)
            return real_record_answer(conv, text)

        monkeypatch.setattr("apps.conversations.forms.record_answer", spy_record_answer)

        record_inbound_whatsapp_message(
            contact=generic.contact,
            wa_contact=wa_contact,
            whatsapp_conversation=wa_convo,
            message_log=log,
        )

        response.refresh_from_db()
        assert response.status == FormResponse.Status.COMPLETED
        # record_answer must not have been called (nfm_reply was consumed first)
        assert record_answer_calls == []


# ---------------------------------------------------------------------------
# ConversationForm.note_questions_changed
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestNoteQuestionsChanged:
    def test_flips_published_to_needs_republish_on_content_change(self, account):
        from apps.conversations.flow_json import build, content_hash

        form = ConversationForm.objects.create(
            account=account,
            name="NQC",
            questions=QUESTIONS,
            presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
            flow_status=ConversationForm.FlowStatus.PUBLISHED,
            flow_json_hash=content_hash(build(QUESTIONS)),
        )

        form.questions = QUESTIONS + [
            {
                "key": "phone",
                "label": "Your phone?",
                "field_type": "phone",
                "maps_to": "",
            }
        ]
        form.save(update_fields=["questions", "updated_at"])
        form.note_questions_changed()

        form.refresh_from_db()
        assert form.flow_status == ConversationForm.FlowStatus.NEEDS_REPUBLISH

    def test_does_not_flip_for_text_presentation(self, account):
        from apps.conversations.flow_json import build, content_hash

        form = ConversationForm.objects.create(
            account=account,
            name="TextMode",
            questions=QUESTIONS,
            presentation=ConversationForm.Presentation.TEXT,
            flow_status=ConversationForm.FlowStatus.PUBLISHED,
            flow_json_hash=content_hash(build(QUESTIONS)),
        )

        form.questions = QUESTIONS + [
            {"key": "city", "label": "City?", "field_type": "text", "maps_to": ""}
        ]
        form.save(update_fields=["questions", "updated_at"])
        form.note_questions_changed()

        form.refresh_from_db()
        assert form.flow_status == ConversationForm.FlowStatus.PUBLISHED

    def test_does_not_flip_when_hash_unchanged(self, account):
        from apps.conversations.flow_json import build, content_hash

        form = ConversationForm.objects.create(
            account=account,
            name="SameHash",
            questions=QUESTIONS,
            presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
            flow_status=ConversationForm.FlowStatus.PUBLISHED,
            flow_json_hash=content_hash(build(QUESTIONS)),
        )

        form.questions = list(QUESTIONS)
        form.save(update_fields=["questions", "updated_at"])
        form.note_questions_changed()

        form.refresh_from_db()
        assert form.flow_status == ConversationForm.FlowStatus.PUBLISHED

    def test_does_not_flip_when_not_published(self, account):
        from apps.conversations.flow_json import build, content_hash

        form = ConversationForm.objects.create(
            account=account,
            name="Draft",
            questions=QUESTIONS,
            presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
            flow_status=ConversationForm.FlowStatus.DRAFT,
            flow_json_hash=content_hash(build(QUESTIONS)),
        )

        form.questions = QUESTIONS + [
            {"key": "x", "label": "X?", "field_type": "text", "maps_to": ""}
        ]
        form.save(update_fields=["questions", "updated_at"])
        form.note_questions_changed()

        form.refresh_from_db()
        assert form.flow_status == ConversationForm.FlowStatus.DRAFT


# ---------------------------------------------------------------------------
# Settings-view: set_presentation and publish_flow POSTs
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSettingsViewFlowActions:
    @pytest.fixture
    def owner_client(self, client, account):
        user = User.objects.create_user("owner", "o@example.com", "pw")
        Membership.objects.create(
            user=user, account=account, role=Membership.Role.OWNER
        )
        client.force_login(user)
        return client

    def test_set_presentation_to_whatsapp_flow(self, owner_client, account):
        form = ConversationForm.objects.create(
            account=account, name="Pres", questions=QUESTIONS
        )
        owner_client.post(
            f"/inbox/forms/{form.pk}/",
            {"action": "set_presentation", "presentation": "whatsapp_flow"},
        )
        form.refresh_from_db()
        assert form.presentation == ConversationForm.Presentation.WHATSAPP_FLOW

    def test_set_presentation_back_to_text(self, owner_client, account):
        form = ConversationForm.objects.create(
            account=account,
            name="Pres2",
            questions=QUESTIONS,
            presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
        )
        owner_client.post(
            f"/inbox/forms/{form.pk}/",
            {"action": "set_presentation", "presentation": "text"},
        )
        form.refresh_from_db()
        assert form.presentation == ConversationForm.Presentation.TEXT

    def test_set_presentation_ignores_invalid_value(self, owner_client, account):
        form = ConversationForm.objects.create(
            account=account,
            name="Pres3",
            questions=QUESTIONS,
            presentation=ConversationForm.Presentation.TEXT,
        )
        owner_client.post(
            f"/inbox/forms/{form.pk}/",
            {"action": "set_presentation", "presentation": "carrier_pigeon"},
        )
        form.refresh_from_db()
        assert form.presentation == ConversationForm.Presentation.TEXT

    def test_publish_flow_post_calls_orchestrator_and_shows_success_message(
        self, owner_client, account
    ):
        form = ConversationForm.objects.create(
            account=account,
            name="PubFlow",
            questions=QUESTIONS,
            presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
        )

        def fake_publish(f):
            f.flow_status = ConversationForm.FlowStatus.PUBLISHED
            f.save(update_fields=["flow_status", "updated_at"])

        with patch(
            "apps.whatsapp.tasks.publish_conversation_flow", side_effect=fake_publish
        ):
            resp = owner_client.post(
                f"/inbox/forms/{form.pk}/",
                {"action": "publish_flow"},
                follow=True,
            )

        assert resp.status_code == 200
        messages = list(resp.context["messages"])
        assert any("published" in str(m).lower() for m in messages)

    def test_publish_flow_post_shows_error_message_on_failure(
        self, owner_client, account
    ):
        form = ConversationForm.objects.create(
            account=account,
            name="PubFail",
            questions=QUESTIONS,
            presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
        )

        def fake_publish_error(f):
            f.flow_status = ConversationForm.FlowStatus.ERROR
            f.flow_error = "Something went wrong"
            f.save(update_fields=["flow_status", "flow_error", "updated_at"])

        with patch(
            "apps.whatsapp.tasks.publish_conversation_flow",
            side_effect=fake_publish_error,
        ):
            resp = owner_client.post(
                f"/inbox/forms/{form.pk}/",
                {"action": "publish_flow"},
                follow=True,
            )

        assert resp.status_code == 200
        messages = list(resp.context["messages"])
        assert any("Something went wrong" in str(m) for m in messages)
