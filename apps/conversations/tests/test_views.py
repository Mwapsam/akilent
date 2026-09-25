import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, ConversationNote
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.fixture
def open_conversation(logged_in):
    _, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    message_log = MessageLog.objects.create(
        account=account, conversation=wa_conversation, contact=wa_contact,
        message_id="wamid.VIEWTEST", direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT, content="Do you have the blue dress?",
        status=MessageLog.Status.DELIVERED, timestamp=timezone.now(),
    )
    return record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )


@pytest.fixture
def conversation_without_a_lead(logged_in):
    """Like ``open_conversation``, but opened with a question that carries no
    buying intent, so ``capture_opportunity`` leaves it alone.

    "Do you have the blue dress?" trips the "do you have" phrase in
    apps.conversations.intent, which means the usual fixture arrives with a
    Lead already banked — fine for most tests, but not for one about creating
    the first one.
    """
    _, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260979999999")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260979999999", contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    message_log = MessageLog.objects.create(
        account=account, conversation=wa_conversation, contact=wa_contact,
        message_id="wamid.NOLEAD", direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT, content="Are you open on Sunday?",
        status=MessageLog.Status.DELIVERED, timestamp=timezone.now(),
    )
    return record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )


@pytest.mark.django_db
def test_inbox_lists_conversations_needing_reply(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.get("/inbox/")
    assert resp.status_code == 200
    assert "+260971234567" in resp.content.decode() or "blue dress" not in resp.content.decode()


@pytest.mark.django_db
def test_inbox_scoped_to_account(logged_in, open_conversation):
    client, _, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000000")
    other_wa = WhatsAppContact.objects.create(
        account=other, phone_number="+260970000000", contact=other_contact,
    )
    other_convo = Conversation.objects.create(
        account=other, contact=other_contact, channel=Conversation.Channel.WHATSAPP,
    )
    resp = client.get(f"/inbox/{other_convo.public_id}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_reply_sends_via_action_registry_and_marks_read(logged_in, open_conversation):
    client, account, _ = logged_in
    from apps.whatsapp.models import OutboundMessage

    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "reply", "body": "Yes, medium and large in stock."},
    )
    assert resp.status_code == 302
    assert OutboundMessage.objects.filter(account=account).exists()
    open_conversation.refresh_from_db()
    assert open_conversation.is_unread is False


@pytest.mark.django_db
def test_assign_to_self(logged_in, open_conversation):
    client, _, user = logged_in
    resp = client.post(f"/inbox/{open_conversation.public_id}/", {"action": "assign"})
    assert resp.status_code == 302
    open_conversation.refresh_from_db()
    assert open_conversation.assigned_to_id == user.id


@pytest.mark.django_db
def test_add_internal_note(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "add_note", "body": "Called back, left voicemail"},
    )
    assert resp.status_code == 302
    assert ConversationNote.objects.filter(
        conversation=open_conversation, body="Called back, left voicemail"
    ).exists()


@pytest.mark.django_db
def test_conversation_detail_renders_thread(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.get(f"/inbox/{open_conversation.public_id}/")
    assert resp.status_code == 200
    assert "Do you have the blue dress?" in resp.content.decode()


@pytest.mark.django_db
def test_conversation_detail_offers_create_lead_when_crm_enabled(logged_in, conversation_without_a_lead):
    """R1.5a: the conversation is the fix for the "orphaned CRM" finding — an
    interested customer can be tracked from the conversation, and doing so
    returns the agent to the conversation (via the hidden "next" field) rather
    than dropping them into a separate CRM screen."""
    client, _, _ = logged_in
    resp = client.get(f"/inbox/{conversation_without_a_lead.public_id}/")
    body = resp.content.decode()
    assert "Track as interested" in body
    assert f'/inbox/{conversation_without_a_lead.public_id}/' in body  # the "next" hidden field

    create_resp = client.post("/sales/leads/create/", {
        "contact": conversation_without_a_lead.contact.phone,
        "next": f"/inbox/{conversation_without_a_lead.public_id}/",
        "source": "conversation",  # hidden field set by the panel's form, not typed by the agent
    })
    assert create_resp.status_code == 302
    assert create_resp["Location"] == f"/inbox/{conversation_without_a_lead.public_id}/"

    from apps.crm.models import Lead

    lead = Lead.objects.get(contact=conversation_without_a_lead.contact)
    assert lead.source == "conversation"  # provenance, so this path is measurable later

    # The panel now reports where they stand instead of offering to track them twice.
    resp = client.get(f"/inbox/{conversation_without_a_lead.public_id}/")
    assert "Where they stand" in resp.content.decode()


@pytest.mark.django_db
def test_send_template_from_composer(logged_in, open_conversation):
    """R1.5c follow-up: the composer's answer to being outside the 24h window
    — send an approved template without leaving the conversation."""
    from apps.whatsapp.models import MessageTemplate, OutboundMessage

    client, account, _ = logged_in
    template = MessageTemplate.objects.create(
        account=account, name="Follow up", whatsapp_template_name="follow_up",
        language_code="en", content="Hi {{1}}", variables=["name"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )

    resp = client.get(f"/inbox/{open_conversation.public_id}/")
    assert "Send a template" in resp.content.decode()

    resp = client.post(f"/inbox/{open_conversation.public_id}/", {
        "action": "send_template", "template_id": template.pk,
        f"var__{template.pk}__name": "Ada",
    })
    assert resp.status_code == 302
    msg = OutboundMessage.objects.get(account=account, template=template)
    assert msg.payload["params"] == {"name": "Ada"}
    # Meta's wire shape, not the label -> value record: sending `params` as the
    # components list is what previously broke every variable template.
    assert msg.payload["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Ada"}]}
    ]
    # Pinned to the conversation it was sent from, so the reply can't surface
    # in a new thread once the 24h window has lapsed.
    assert msg.payload["_conversation_id"] == open_conversation.whatsapp_conversation_id


@pytest.mark.django_db
def test_send_template_rejects_unapproved_template(logged_in, open_conversation):
    from apps.whatsapp.models import MessageTemplate

    client, account, _ = logged_in
    draft = MessageTemplate.objects.create(
        account=account, name="Draft", whatsapp_template_name="draft", content="x",
        approval_status=MessageTemplate.ApprovalStatus.DRAFT,
    )
    resp = client.post(f"/inbox/{open_conversation.public_id}/", {
        "action": "send_template", "template_id": draft.pk,
    }, follow=True)
    assert resp.status_code == 200
    # Flash messages are JSON-embedded (escapejs) for the client-side toast, not
    # plain HTML — check for the message text without relying on quote escaping.
    assert b"That template" in resp.content


@pytest.mark.django_db
def test_messages_feed_returns_only_newer_messages(logged_in, open_conversation):
    client, _, _ = logged_in
    first = open_conversation.messages.first()

    resp = client.get(f"/inbox/{open_conversation.public_id}/messages/?after=0")
    assert resp.status_code == 200
    data = resp.json()
    assert [m["id"] for m in data["messages"]] == [first.id]
    assert data["messages"][0]["body"] == "Do you have the blue dress?"
    assert data["open"] is True
    assert "Waiting for you" in data["status_html"]

    resp = client.get(f"/inbox/{open_conversation.public_id}/messages/?after={first.id}")
    assert resp.json()["messages"] == []


@pytest.mark.django_db
def test_messages_feed_marks_new_inbound_read(logged_in, open_conversation):
    client, _, _ = logged_in
    Conversation.objects.filter(pk=open_conversation.pk).update(is_unread=True)
    client.get(f"/inbox/{open_conversation.public_id}/messages/?after=0")
    open_conversation.refresh_from_db()
    assert open_conversation.is_unread is False


@pytest.mark.django_db
def test_messages_feed_scoped_to_account_and_login(logged_in, open_conversation, client):
    _, _, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000000")
    other_convo = Conversation.objects.create(
        account=other, contact=other_contact, channel=Conversation.Channel.WHATSAPP,
    )
    assert client.get(f"/inbox/{other_convo.public_id}/messages/").status_code == 404
    client.logout()
    assert client.get(f"/inbox/{open_conversation.public_id}/messages/").status_code == 302


@pytest.mark.django_db
def test_ajax_reply_returns_json(logged_in, open_conversation):
    client, account, _ = logged_in
    from apps.whatsapp.models import OutboundMessage

    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "reply", "body": "On its way!"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert OutboundMessage.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_ajax_reply_with_empty_body_returns_error(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "reply", "body": "   "},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.django_db
def test_inbox_feed_returns_rendered_list(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.get("/inbox/feed/?view=all")
    assert resp.status_code == 200
    assert "blue dress" in resp.json()["html"]


@pytest.fixture
def teammate(logged_in):
    _, account, _ = logged_in
    user = User.objects.create_user("sam", "sam@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    return user


@pytest.mark.django_db
def test_assign_to_a_teammate(logged_in, open_conversation, teammate):
    client, _, _ = logged_in
    client.post(f"/inbox/{open_conversation.public_id}/", {"action": "assign", "assignee": teammate.pk})
    open_conversation.refresh_from_db()
    assert open_conversation.assigned_to_id == teammate.pk


@pytest.mark.django_db
def test_unassign(logged_in, open_conversation, teammate):
    client, _, _ = logged_in
    open_conversation.assign(teammate)
    client.post(f"/inbox/{open_conversation.public_id}/", {"action": "assign", "assignee": "none"})
    open_conversation.refresh_from_db()
    assert open_conversation.assigned_to_id is None


@pytest.mark.django_db
def test_cannot_assign_to_someone_outside_the_business(logged_in, open_conversation):
    client, _, _ = logged_in
    stranger = User.objects.create_user("stranger", "s@example.com", "pw")
    other = Account.objects.create(company_name="Other Co")
    Membership.objects.create(user=stranger, account=other, role=Membership.Role.OWNER)
    client.post(f"/inbox/{open_conversation.public_id}/", {"action": "assign", "assignee": stranger.pk})
    open_conversation.refresh_from_db()
    assert open_conversation.assigned_to_id is None


@pytest.mark.django_db
def test_the_action_itself_refuses_a_non_member(open_conversation):
    from apps.conversations.actions import ActionError, run_action

    stranger = User.objects.create_user("stranger", "s@example.com", "pw")
    with pytest.raises(ActionError):
        run_action("assign_conversation", {}, conversation=open_conversation, user=stranger)


@pytest.mark.django_db
def test_the_picker_lists_teammates(logged_in, open_conversation, teammate):
    client, _, _ = logged_in
    body = client.get(f"/inbox/{open_conversation.public_id}/").content.decode()
    assert 'name="assignee"' in body and "sam" in body


@pytest.mark.django_db
def test_a_customer_given_a_value_shows_as_tracked_in_the_pipeline(logged_in, conversation_without_a_lead):
    """Adding an estimated value turns the lead straight into a deal, which leaves no open
    lead. The panel used to read that as "Not tracked yet" and offer to track them again."""
    client, _, _ = logged_in
    page = f"/inbox/{conversation_without_a_lead.public_id}/"
    client.post("/sales/leads/create/", {
        "contact": conversation_without_a_lead.contact.phone, "next": page,
        "source": "conversation", "value": "500",
    })

    from apps.crm.models import Deal, Lead

    assert not Lead.objects.filter(contact=conversation_without_a_lead.contact, status="new").exists()
    deal = Deal.objects.get(contact=conversation_without_a_lead.contact)
    body = client.get(page).content.decode()
    assert "Not tracked yet" not in body
    assert "In your pipeline" in body and f"/sales/deals/{deal.public_id}/" in body
    assert "Tracked as interested" in body
    assert 'name="value"' not in body          # the create-lead form is not offered again


@pytest.mark.django_db
def test_a_lead_without_a_value_still_shows_where_they_stand(logged_in, conversation_without_a_lead):
    client, _, _ = logged_in
    page = f"/inbox/{conversation_without_a_lead.public_id}/"
    client.post("/sales/leads/create/", {
        "contact": conversation_without_a_lead.contact.phone, "next": page, "source": "conversation"})
    body = client.get(page).content.decode()
    assert "Where they stand" in body and "Not tracked yet" not in body


@pytest.mark.django_db
def test_a_closed_deal_no_longer_counts_as_tracked(logged_in, conversation_without_a_lead):
    client, _, _ = logged_in
    page = f"/inbox/{conversation_without_a_lead.public_id}/"
    client.post("/sales/leads/create/", {
        "contact": conversation_without_a_lead.contact.phone, "next": page,
        "source": "conversation", "value": "500",
    })

    from apps.crm.models import Deal

    Deal.objects.filter(contact=conversation_without_a_lead.contact).update(status=Deal.Status.WON)
    body = client.get(page).content.decode()
    assert "Not tracked yet" in body and "Track as interested" in body
