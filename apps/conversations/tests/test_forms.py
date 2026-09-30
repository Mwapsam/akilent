"""Phase C item 11: native WhatsApp forms — apps.conversations.forms, the
start_conversation_form action, the settings screens, and the inbound-message
hook that stops a form answer from also triggering AI/keyword workflows."""

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import forms as conversation_forms
from apps.conversations.actions import ActionError, run_action
from apps.conversations.models import (
    Conversation,
    ConversationForm,
    Event,
    FormResponse,
    Message,
)
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact

INTAKE_QUESTIONS = [
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
        "maps_to": "contact.attributes.email",
    },
]


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def form(account):
    return ConversationForm.objects.create(
        account=account,
        name="Intake",
        status=ConversationForm.Status.PUBLISHED,
        questions=INTAKE_QUESTIONS,
    )


@pytest.fixture
def conversation(account):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    return Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )


def _reply_action_stub(monkeypatch):
    """The tests here care about form state, not real WhatsApp sends -- replace the
    ``reply`` action with a no-op that just records what it was called with."""
    sent = []

    def fake_run_action(name, context, **kwargs):
        if name == "reply":
            sent.append(kwargs["body"])
            return {"outbound_message_id": None}
        return run_action(name, context, **kwargs)

    # forms._send() does ``from apps.conversations.actions import run_action`` inside the
    # function, so patching that module's attribute is what the call picks up.
    monkeypatch.setattr("apps.conversations.actions.run_action", fake_run_action)
    return sent


@pytest.mark.django_db
class TestFormRuntime:
    def test_starting_a_form_sends_the_first_question(
        self, monkeypatch, conversation, form
    ):
        sent = _reply_action_stub(monkeypatch)
        response = conversation_forms.start_form(conversation, form)
        assert response.status == FormResponse.Status.IN_PROGRESS
        assert response.current_index == 0
        assert sent == ["What's your name?"]

    def test_starting_a_form_with_no_questions_raises(self, conversation, account):
        empty = ConversationForm.objects.create(account=account, name="Empty")
        with pytest.raises(conversation_forms.FormError):
            conversation_forms.start_form(conversation, empty)

    def test_full_happy_path_completes_and_maps_answers(
        self, monkeypatch, conversation, form
    ):
        sent = _reply_action_stub(monkeypatch)
        conversation_forms.start_form(conversation, form)

        assert conversation_forms.record_answer(conversation, "Chanda") is True
        assert sent[-1] == "What's your email?"

        assert (
            conversation_forms.record_answer(conversation, "chanda@example.com") is True
        )
        assert sent[-1] == "Thanks - got everything I need!"

        response = FormResponse.objects.get(conversation=conversation)
        assert response.status == FormResponse.Status.COMPLETED
        assert response.completed_at is not None
        assert response.answers == {"name": "Chanda", "email": "chanda@example.com"}

        conversation.contact.refresh_from_db()
        assert conversation.contact.first_name == "Chanda"
        assert conversation.contact.attributes.get("email") == "chanda@example.com"

        assert Event.objects.filter(type="conversation.form_completed").exists()

    def test_an_invalid_answer_is_re_prompted_without_advancing(
        self, monkeypatch, conversation, form
    ):
        sent = _reply_action_stub(monkeypatch)
        conversation_forms.start_form(conversation, form)
        conversation_forms.record_answer(conversation, "Chanda")

        assert conversation_forms.record_answer(conversation, "not-an-email") is True
        response = FormResponse.objects.get(conversation=conversation)
        assert response.current_index == 1  # unchanged
        assert response.status == FormResponse.Status.IN_PROGRESS
        assert "email" not in response.answers
        assert "doesn't look like an email" in sent[-1]

        # A valid answer after the retry still completes it.
        conversation_forms.record_answer(conversation, "chanda@example.com")
        response.refresh_from_db()
        assert response.status == FormResponse.Status.COMPLETED

    def test_no_active_form_returns_false(self, conversation):
        assert conversation_forms.record_answer(conversation, "hello") is False

    def test_active_response_for_only_returns_in_progress(
        self, monkeypatch, conversation, form
    ):
        _reply_action_stub(monkeypatch)
        response = conversation_forms.start_form(conversation, form)
        assert conversation_forms.active_response_for(conversation) == response
        response.status = FormResponse.Status.COMPLETED
        response.save(update_fields=["status"])
        assert conversation_forms.active_response_for(conversation) is None

    def test_a_second_answer_for_a_number_field_type_rejects_text(
        self, monkeypatch, conversation, account
    ):
        sent = _reply_action_stub(monkeypatch)
        numeric_form = ConversationForm.objects.create(
            account=account,
            name="Quote",
            status=ConversationForm.Status.PUBLISHED,
            questions=[
                {
                    "key": "qty",
                    "label": "How many units?",
                    "field_type": "number",
                    "maps_to": "",
                }
            ],
        )
        conversation_forms.start_form(conversation, numeric_form)
        assert conversation_forms.record_answer(conversation, "a dozen") is True
        response = FormResponse.objects.get(conversation=conversation)
        assert response.status == FormResponse.Status.IN_PROGRESS
        assert "doesn't look like a number" in sent[-1]
        conversation_forms.record_answer(conversation, "12")
        response.refresh_from_db()
        assert response.answers == {"qty": "12"}


@pytest.mark.django_db
class TestStartConversationFormAction:
    def test_refuses_a_second_form_while_one_is_in_progress(
        self, monkeypatch, conversation, form, account
    ):
        _reply_action_stub(monkeypatch)
        conversation_forms.start_form(conversation, form)
        other = ConversationForm.objects.create(
            account=account,
            name="Other",
            status=ConversationForm.Status.PUBLISHED,
            questions=INTAKE_QUESTIONS,
        )
        with pytest.raises(ActionError):
            run_action(
                "start_conversation_form", {}, conversation=conversation, form=other
            )

    def test_run_action_starts_a_form(self, monkeypatch, conversation, form):
        sent = _reply_action_stub(monkeypatch)
        result = run_action(
            "start_conversation_form", {}, conversation=conversation, form=form
        )
        assert FormResponse.objects.filter(pk=result["response_id"]).exists()
        assert sent == ["What's your name?"]


@pytest.mark.django_db
class TestInboundMessageHook:
    @pytest.fixture
    def wa_conversation(self, account):
        """A generic Conversation properly linked to a whatsapp.Conversation, since
        ``record_inbound_whatsapp_message`` resolves its own via
        ``Conversation.get_or_create_for_whatsapp`` — an unlinked ``conversation``
        fixture would make it operate on a second, different Conversation row."""
        contact = Contact.objects.create(account=account, phone="+260971111111")
        wa_contact = WhatsAppContact.objects.create(
            account=account, phone_number=contact.phone, contact=contact
        )
        wa_convo = WhatsAppConversation.get_or_open(wa_contact)
        generic = Conversation.get_or_create_for_whatsapp(wa_convo)
        return wa_contact, wa_convo, generic

    def _log(self, account, wa_contact, wa_convo, content, message_id):
        return MessageLog.objects.create(
            account=account,
            conversation=wa_convo,
            contact=wa_contact,
            message_id=message_id,
            direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.TEXT,
            content=content,
            status=MessageLog.Status.DELIVERED,
            timestamp=timezone.now(),
        )

    def test_a_form_answer_does_not_queue_an_ai_proposal(
        self, monkeypatch, account, form, wa_conversation
    ):
        from apps.ai.models import AIProposal, AISettings

        AISettings.objects.create(account=account, enabled=True)
        wa_contact, wa_convo, generic = wa_conversation
        _reply_action_stub(monkeypatch)
        conversation_forms.start_form(generic, form)

        log = self._log(account, wa_contact, wa_convo, "Chanda", "wamid.FORMANSWER1")
        record_inbound_whatsapp_message(
            contact=generic.contact,
            wa_contact=wa_contact,
            whatsapp_conversation=wa_convo,
            message_log=log,
        )
        assert not AIProposal.objects.filter(
            trigger_message__whatsapp_message=log
        ).exists()

    def test_a_form_answer_is_recorded_as_the_inbound_message(
        self, monkeypatch, account, form, wa_conversation
    ):
        wa_contact, wa_convo, generic = wa_conversation
        _reply_action_stub(monkeypatch)
        conversation_forms.start_form(generic, form)

        log = self._log(account, wa_contact, wa_convo, "Chanda", "wamid.FORMANSWER2")
        record_inbound_whatsapp_message(
            contact=generic.contact,
            wa_contact=wa_contact,
            whatsapp_conversation=wa_convo,
            message_log=log,
        )
        assert Message.objects.filter(whatsapp_message=log).exists()
        response = FormResponse.objects.get(conversation=generic)
        assert response.current_index == 1


@pytest.mark.django_db
class TestSettingsViews:
    @pytest.fixture
    def owner(self, client, account):
        user = User.objects.create_user("owner", "o@example.com", "pw")
        Membership.objects.create(
            user=user, account=account, role=Membership.Role.OWNER
        )
        client.force_login(user)
        return client, user

    def test_creating_a_form(self, owner, account):
        client, _ = owner
        resp = client.post("/inbox/forms/", {"name": "Intake"})
        assert resp.status_code == 302
        assert ConversationForm.objects.filter(account=account, name="Intake").exists()

    def test_adding_a_question(self, owner, account):
        client, _ = owner
        form = ConversationForm.objects.create(account=account, name="Intake")
        client.post(
            f"/inbox/forms/{form.pk}/",
            {
                "action": "add_question",
                "label": "What's your name?",
                "field_type": "text",
                "maps_to_type": "first_name",
            },
        )
        form.refresh_from_db()
        assert form.questions == [
            {
                "key": "whats_your_name",
                "label": "What's your name?",
                "field_type": "text",
                "maps_to": "contact.first_name",
            }
        ]

    def test_a_custom_field_mapping(self, owner, account):
        client, _ = owner
        form = ConversationForm.objects.create(account=account, name="Intake")
        client.post(
            f"/inbox/forms/{form.pk}/",
            {
                "action": "add_question",
                "label": "Company?",
                "field_type": "text",
                "maps_to_type": "custom",
                "maps_to_key": "Company Name",
            },
        )
        form.refresh_from_db()
        assert form.questions[0]["maps_to"] == "contact.attributes.company_name"

    def test_duplicate_question_labels_get_distinct_keys(self, owner, account):
        client, _ = owner
        form = ConversationForm.objects.create(account=account, name="Intake")
        for _ in range(2):
            client.post(
                f"/inbox/forms/{form.pk}/",
                {"action": "add_question", "label": "Name?", "field_type": "text"},
            )
        form.refresh_from_db()
        keys = [q["key"] for q in form.questions]
        assert keys == ["name", "name_2"]

    def test_removing_a_question(self, owner, account):
        client, _ = owner
        form = ConversationForm.objects.create(
            account=account, name="Intake", questions=list(INTAKE_QUESTIONS)
        )
        client.post(
            f"/inbox/forms/{form.pk}/", {"action": "remove_question", "index": "0"}
        )
        form.refresh_from_db()
        assert form.questions == [INTAKE_QUESTIONS[1]]

    def test_cannot_publish_with_no_questions(self, owner, account):
        client, _ = owner
        form = ConversationForm.objects.create(account=account, name="Intake")
        client.post(
            f"/inbox/forms/{form.pk}/", {"action": "set_status", "status": "published"}
        )
        form.refresh_from_db()
        assert form.status == ConversationForm.Status.DRAFT

    def test_publishing_with_questions(self, owner, account):
        client, _ = owner
        form = ConversationForm.objects.create(
            account=account, name="Intake", questions=list(INTAKE_QUESTIONS)
        )
        client.post(
            f"/inbox/forms/{form.pk}/", {"action": "set_status", "status": "published"}
        )
        form.refresh_from_db()
        assert form.status == ConversationForm.Status.PUBLISHED

    def test_deleting_a_form(self, owner, account):
        client, _ = owner
        form = ConversationForm.objects.create(account=account, name="Intake")
        client.post(f"/inbox/forms/{form.pk}/delete/")
        assert not ConversationForm.objects.filter(pk=form.pk).exists()

    def test_cannot_manage_another_accounts_form(self, owner):
        client, _ = owner
        other = Account.objects.create(company_name="Other Co")
        other_form = ConversationForm.objects.create(account=other, name="Not yours")
        resp = client.get(f"/inbox/forms/{other_form.pk}/")
        assert resp.status_code == 404
