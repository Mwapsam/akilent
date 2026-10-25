"""Forms and WhatsApp Flows, production-ready (Phase 5 of the 2026-10-24 automation plan)."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import flow_json
from apps.conversations import forms as conversation_forms
from apps.conversations.models import Conversation, ConversationForm, FormResponse
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import WhatsAppContact

RICH = [
    {
        "key": "name",
        "label": "Your name",
        "field_type": "text",
        "maps_to": "contact.first_name",
    },
    {
        "key": "size",
        "label": "Size",
        "field_type": "choice",
        "options": ["Small", "Large"],
    },
    {
        "key": "extras",
        "label": "Extras",
        "field_type": "multi_choice",
        "options": ["Gift wrap", "Card"],
        "required": False,
    },
    {"key": "when", "label": "Delivery date", "field_type": "date", "page_break": True},
    {
        "key": "ok",
        "label": "I agree to be contacted",
        "field_type": "opt_in",
        "maps_to": "contact.attributes.consent",
    },
]


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def wa_conversation(account):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number=contact.phone, contact=contact
    )
    return Conversation.get_or_create_for_whatsapp(
        WhatsAppConversation.get_or_open(wa_contact)
    )


def _form(account, questions=RICH, **kw):
    kw.setdefault("status", ConversationForm.Status.PUBLISHED)
    return ConversationForm.objects.create(
        account=account, name="Order details", questions=questions, **kw
    )


# ── Flow JSON ────────────────────────────────────────────────────────────────


def test_the_flow_uses_a_publishable_version_and_rich_components():
    flow = flow_json.build(RICH, title="Order details", intro="A few quick questions")
    assert flow["version"] == "7.3"
    first, second = flow["screens"]
    kinds = [c["type"] for c in first["layout"]["children"][0]["children"]]
    assert kinds == [
        "TextBody",
        "TextInput",
        "RadioButtonsGroup",
        "CheckboxGroup",
        "Footer",
    ]
    assert (
        first["layout"]["children"][0]["children"][-1]["on-click-action"]["name"]
        == "navigate"
    )
    # Page two receives page one's answers and completes with all of them.
    assert set(second["data"]) == {"name", "size", "extras"}
    footer = second["layout"]["children"][0]["children"][-1]
    assert footer["on-click-action"]["name"] == "complete"
    assert footer["on-click-action"]["payload"]["name"] == "${data.name}"
    assert footer["on-click-action"]["payload"]["when"] == "${form.when}"
    assert second.get("terminal") and second.get("success")


def test_check_explains_what_meta_would_reject():
    problems = flow_json.check(
        [
            {
                "key": "a",
                "label": "What is your full delivery address?",
                "field_type": "text",
            },
            {
                "key": "b",
                "label": "Pick",
                "field_type": "choice",
                "options": ["Only one"],
            },
        ]
    )
    assert any("at most 20" in p for p in problems)
    assert any("two choices" in p for p in problems)


def test_choice_answers_map_back_from_option_ids():
    assert flow_json.option_title(RICH[1], "opt_1") == "Large"


# ── Answers ──────────────────────────────────────────────────────────────────


def test_typed_answers_accept_numbers_names_and_skip(account, wa_conversation):
    form = _form(account, presentation=ConversationForm.Presentation.TEXT)
    sent = []
    with patch(
        "apps.conversations.forms._send", side_effect=lambda c, t, **k: sent.append(t)
    ):
        conversation_forms.start_form(wa_conversation, form)
        for answer in ["Ada", "2", "skip", "25/12/2026", "yes"]:
            assert conversation_forms.record_answer(wa_conversation, answer)
    response = FormResponse.objects.get()
    assert response.status == FormResponse.Status.COMPLETED
    assert response.answers == {
        "name": "Ada",
        "size": "Large",
        "when": "2026-12-25",
        "ok": True,
    }
    assert "1. Small" in sent[1]  # choices are listed
    contact = wa_conversation.contact
    contact.refresh_from_db()
    assert contact.first_name == "Ada" and contact.attributes["consent"] is True


def test_an_invalid_choice_is_asked_again(account, wa_conversation):
    form = _form(account, presentation=ConversationForm.Presentation.TEXT)
    sent = []
    with patch(
        "apps.conversations.forms._send", side_effect=lambda c, t, **k: sent.append(t)
    ):
        conversation_forms.start_form(wa_conversation, form)
        conversation_forms.record_answer(wa_conversation, "Ada")
        conversation_forms.record_answer(wa_conversation, "Medium")
    assert "pick one of the choices" in sent[-1]
    assert FormResponse.objects.get().current_index == 1


def test_flow_answers_with_lists_and_booleans(account, wa_conversation):
    form = _form(
        account,
        presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
        flow_id="F1",
        flow_status=ConversationForm.FlowStatus.PUBLISHED,
        thanks_text="Thank you!",
    )
    sent_texts = []
    with (
        patch("apps.whatsapp.api.send_interactive") as send,
        patch(
            "apps.conversations.forms._send",
            side_effect=lambda c, t, **k: sent_texts.append(t),
        ),
    ):
        response = conversation_forms.start_form(wa_conversation, form)
        payload = send.call_args.args[2]
        assert payload["action"]["parameters"]["flow_message_version"] == "3"
        assert conversation_forms.complete_from_flow(
            wa_conversation,
            response.flow_token,
            {
                "name": "Ada",
                "size": "opt_0",
                "extras": ["opt_0", "opt_1"],
                "when": "2026-12-25",
                "ok": True,
            },
        )
    response.refresh_from_db()
    assert response.answers["extras"] == ["Gift wrap", "Card"]
    assert response.answers["size"] == "Small"
    assert sent_texts == ["Thank you!"]


def test_a_template_button_flow_completes_without_a_prior_response(
    account, wa_conversation
):
    form = _form(
        account, flow_id="F2", flow_status=ConversationForm.FlowStatus.PUBLISHED
    )
    token = conversation_forms.new_flow_token(form)
    with patch("apps.conversations.forms._send"):
        assert conversation_forms.complete_from_flow(
            wa_conversation, token, {"name": "Bo"}
        )
    response = FormResponse.objects.get()
    assert response.form == form and response.status == FormResponse.Status.COMPLETED


def test_a_token_from_another_business_is_ignored(account, wa_conversation):
    other = Account.objects.create(company_name="Other")
    foreign = _form(other, flow_id="F3")
    token = conversation_forms.new_flow_token(foreign)
    assert (
        conversation_forms.complete_from_flow(wa_conversation, token, {"name": "X"})
        is False
    )


def test_lead_action_tracks_and_qualifies(account, wa_conversation):
    from apps.crm.models import Lead

    form = _form(
        account,
        questions=[
            {
                "key": "budget",
                "label": "Budget",
                "field_type": "number",
                "maps_to": "lead.attributes.budget",
            }
        ],
        presentation=ConversationForm.Presentation.TEXT,
        lead_action=ConversationForm.LeadAction.QUALIFY,
    )
    with patch("apps.conversations.forms._send"):
        conversation_forms.start_form(wa_conversation, form)
        conversation_forms.record_answer(wa_conversation, "500")
    lead = Lead.objects.get(contact=wa_conversation.contact)
    assert lead.status == "qualified" and lead.attributes["budget"] == "500"


def test_stale_forms_are_let_go(account, wa_conversation):
    form = _form(
        account, presentation=ConversationForm.Presentation.TEXT, timeout_hours=2
    )
    with patch("apps.conversations.forms._send"):
        response = conversation_forms.start_form(wa_conversation, form)
    FormResponse.objects.filter(pk=response.pk).update(
        started_at=timezone.now() - timedelta(hours=3)
    )
    assert conversation_forms.abandon_stale() == 1
    response.refresh_from_db()
    assert response.status == FormResponse.Status.ABANDONED


# ── Automations ──────────────────────────────────────────────────────────────


def _wf(account, definition, slug="wf"):
    from apps.automation.models import Workflow

    return Workflow.objects.create(
        account=account,
        name=slug,
        slug=slug,
        status=Workflow.Status.PUBLISHED,
        definition=definition,
    )


def test_a_start_form_step_waits_then_continues_with_the_answers(
    account, wa_conversation
):
    from apps.automation.models import WorkflowRun
    from apps.automation.workflow_engine import enroll

    form = _form(
        account,
        questions=[{"key": "budget", "label": "Budget", "field_type": "number"}],
        presentation=ConversationForm.Presentation.TEXT,
    )
    wf = _wf(
        account,
        {
            "trigger": {"type": "manual"},
            "steps": [
                {
                    "id": "ask",
                    "type": "start_form",
                    "form": form.public_id,
                    "next": "tag",
                    "on_timeout": "",
                },
                {"id": "tag", "type": "add_tag", "tag": "answered"},
            ],
        },
    )
    with patch("apps.conversations.forms._send"):
        run = enroll(
            wf,
            wa_conversation.contact,
            context={
                "conversation_id": wa_conversation.public_id,
                "message": {"body": "hi"},
            },
        )
        run.refresh_from_db()
        assert run.status == WorkflowRun.Status.WAITING
        conversation_forms.record_answer(wa_conversation, "300")
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    assert run.context["form_answers"] == {"budget": "300"}
    assert wa_conversation.contact.tags.filter(slug="answered").exists()


def test_a_form_completed_trigger_can_target_one_form(account, wa_conversation):
    from apps.automation.models import WorkflowRun

    form = _form(
        account,
        questions=[{"key": "a", "label": "A", "field_type": "text"}],
        presentation="text",
    )
    other = _form(
        account,
        questions=[{"key": "a", "label": "A", "field_type": "text"}],
        presentation="text",
    )
    _wf(
        account,
        {
            "trigger": {"type": "conversation.form_completed", "form": other.public_id},
            "steps": [{"id": "t", "type": "add_tag", "tag": "x"}],
        },
    )
    with patch("apps.conversations.forms._send"):
        conversation_forms.start_form(wa_conversation, form)
        conversation_forms.record_answer(wa_conversation, "hello")
    assert not WorkflowRun.objects.exists()


# ── Publishing ───────────────────────────────────────────────────────────────


def _number(account):
    from apps.whatsapp.models import WhatsAppBusinessNumber

    return WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id="PN1",
        waba_id="WABA1",
        is_active=True,
        access_token="t",
    )


def test_republish_updates_in_place_when_meta_allows(account):
    from apps.whatsapp.tasks import publish_conversation_flow

    _number(account)
    form = _form(
        account,
        flow_id="OLD",
        flow_waba_id="WABA1",
        flow_status=ConversationForm.FlowStatus.NEEDS_REPUBLISH,
    )
    provider = MagicMock()
    with patch("apps.whatsapp.providers.get_whatsapp_provider", return_value=provider):
        publish_conversation_flow(form)
    form.refresh_from_db()
    assert (
        form.flow_status == ConversationForm.FlowStatus.PUBLISHED
        and form.flow_id == "OLD"
    )
    provider.create_flow.assert_not_called()


def test_republish_makes_a_new_flow_when_meta_refuses_the_edit(account):
    from apps.whatsapp.providers.base import WhatsAppProviderError
    from apps.whatsapp.tasks import publish_conversation_flow

    _number(account)
    form = _form(
        account,
        flow_id="OLD",
        flow_waba_id="WABA1",
        flow_status=ConversationForm.FlowStatus.NEEDS_REPUBLISH,
    )
    provider = MagicMock()
    provider.update_flow_json.side_effect = [
        WhatsAppProviderError("published flows can't change"),
        {},
    ]
    provider.create_flow.return_value = {"id": "NEW"}
    with patch("apps.whatsapp.providers.get_whatsapp_provider", return_value=provider):
        publish_conversation_flow(form)
    form.refresh_from_db()
    assert (
        form.flow_id == "NEW"
        and form.flow_status == ConversationForm.FlowStatus.PUBLISHED
    )
    provider.deprecate_flow.assert_called_once_with("OLD")


def test_meta_validation_errors_are_shown(account):
    from apps.whatsapp.providers.base import WhatsAppProviderError
    from apps.whatsapp.tasks import publish_conversation_flow

    _number(account)
    form = _form(account)
    provider = MagicMock()
    provider.create_flow.return_value = {"id": "N"}
    err = WhatsAppProviderError("WhatsApp rejected the form: label too long")
    err.code = "FLOW_VALIDATION"
    provider.update_flow_json.side_effect = err
    with patch("apps.whatsapp.providers.get_whatsapp_provider", return_value=provider):
        publish_conversation_flow(form)
    form.refresh_from_db()
    assert (
        form.flow_status == ConversationForm.FlowStatus.ERROR
        and "label too long" in form.flow_error
    )


def test_upload_validation_errors_raise():
    from apps.whatsapp.providers.base import WhatsAppProviderError
    from apps.whatsapp.providers.meta import MetaCloudAPIProvider

    provider = MetaCloudAPIProvider.__new__(MetaCloudAPIProvider)
    provider._session = MagicMock()
    provider._url = lambda path: "https://graph/" + path
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "success": True,
        "validation_errors": [{"message": "Label too long"}],
    }
    provider._session.post.return_value = response
    with pytest.raises(WhatsAppProviderError, match="Label too long"):
        provider.update_flow_json("F", {"version": "7.3"})


# ── Templates with a form button ─────────────────────────────────────────────


def test_a_template_form_button_sends_a_token_naming_the_form(account):
    from apps.automation.workflows import _build_template_payload
    from apps.whatsapp.models import MessageTemplate

    form = _form(
        account, flow_id="F9", flow_status=ConversationForm.FlowStatus.PUBLISHED
    )
    template = MessageTemplate.objects.create(
        account=account,
        name="Sign up",
        whatsapp_template_name="sign_up",
        content="Tell us about your order",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        buttons=[
            {
                "type": "FLOW",
                "text": "Fill in",
                "flow_id": "F9",
                "flow_action": "navigate",
                "navigate_screen": "FORM",
            }
        ],
    )
    payload = _build_template_payload(template, {})
    button = payload["components"][-1]
    assert button["sub_type"] == "flow"
    token = button["parameters"][0]["action"]["flow_token"]
    assert conversation_forms.form_for_token(account, token) == form


# ── Pages ────────────────────────────────────────────────────────────────────


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("o", "o@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


def test_builder_saves_and_answers_page_exports(owner, account, wa_conversation):
    form = _form(account, questions=[])
    resp = owner.post(
        f"/inbox/forms/{form.pk}/save/",
        json.dumps(
            {"questions": RICH, "copy": {"cta_label": "Begin", "timeout_hours": 24}}
        ),
        content_type="application/json",
    )
    assert resp.status_code == 200 and resp.json()["ok"]
    form.refresh_from_db()
    assert (
        len(form.questions) == 5
        and form.cta_label == "Begin"
        and form.timeout_hours == 24
    )
    assert owner.get(f"/inbox/forms/{form.pk}/").status_code == 200
    FormResponse.objects.create(
        account=account,
        form=form,
        conversation=wa_conversation,
        contact=wa_conversation.contact,
        status=FormResponse.Status.COMPLETED,
        answers={"name": "Ada", "extras": ["Card"]},
    )
    csv = owner.get(f"/inbox/forms/{form.pk}/responses/?format=csv").content.decode()
    assert "Ada" in csv and "Card" in csv
    assert owner.get(f"/inbox/forms/{form.pk}/responses/").status_code == 200
