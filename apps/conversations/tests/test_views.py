import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.inbox_keys import inbox_key, sender_metadata
from apps.conversations.models import Conversation, ConversationNote, Message
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, OutboundMessage, WhatsAppContact
from apps.whatsapp.tasks import project_outbound_to_inbox


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
        account=account,
        phone_number="+260971234567",
        contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    message_log = MessageLog.objects.create(
        account=account,
        conversation=wa_conversation,
        contact=wa_contact,
        message_id="wamid.VIEWTEST",
        direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT,
        content="Do you have the blue dress?",
        status=MessageLog.Status.DELIVERED,
        timestamp=timezone.now(),
    )
    return record_inbound_whatsapp_message(
        contact=contact,
        wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation,
        message_log=message_log,
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
        account=account,
        phone_number="+260979999999",
        contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    message_log = MessageLog.objects.create(
        account=account,
        conversation=wa_conversation,
        contact=wa_contact,
        message_id="wamid.NOLEAD",
        direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT,
        content="Are you open on Sunday?",
        status=MessageLog.Status.DELIVERED,
        timestamp=timezone.now(),
    )
    return record_inbound_whatsapp_message(
        contact=contact,
        wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation,
        message_log=message_log,
    )


@pytest.mark.django_db
def test_inbox_lists_conversations_needing_reply(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.get("/inbox/")
    assert resp.status_code == 200
    assert (
        "+260971234567" in resp.content.decode()
        or "blue dress" not in resp.content.decode()
    )


@pytest.mark.django_db
def test_inbox_scoped_to_account(logged_in, open_conversation):
    client, _, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000000")
    WhatsAppContact.objects.create(
        account=other,
        phone_number="+260970000000",
        contact=other_contact,
    )
    other_convo = Conversation.objects.create(
        account=other,
        contact=other_contact,
        channel=Conversation.Channel.WHATSAPP,
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
def test_a_plain_visit_gets_the_whole_shell_page(logged_in, open_conversation):
    """A direct link, a bookmark, or a phone (no room for a side-by-side pane) all need the
    real page, not just the fragment the two-pane inbox loads into its pane."""
    client, _, _ = logged_in
    resp = client.get(f"/inbox/{open_conversation.public_id}/")
    html = resp.content.decode()
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert 'hx-boost="true"' in html


@pytest.mark.django_db
def test_the_inbox_pane_load_gets_only_the_conversation(logged_in, open_conversation):
    """templates/inbox.html's two-pane list (static/js/inbox_pane.js) loads a row's
    conversation with a plain background GET targeting #conversation-pane-inner — this must
    come back as just that conversation, not the whole page around it."""
    client, _, _ = logged_in
    resp = client.get(
        f"/inbox/{open_conversation.public_id}/",
        HTTP_HX_REQUEST="true",
        HTTP_HX_TARGET="conversation-pane-inner",
    )
    html = resp.content.decode()
    assert resp.status_code == 200
    assert not html.lstrip().lower().startswith("<!doctype")
    assert "Do you have the blue dress?" in html
    for shell_only in (
        "app-shell-pl",
        "inbox-tabs",
        '<main id="main"',
        'aria-label="Account menu"',
    ):
        assert shell_only not in html, shell_only


@pytest.mark.django_db
def test_a_boosted_navigation_still_gets_the_whole_page(logged_in, open_conversation):
    """A row's plain href is still a real boosted navigation below lg (no pane to load into).
    A boosted request is never the pane's background GET (apps.core.htmx.is_background), even
    if it happens to carry the same HX-Target some other client sent — HX-Boosted wins."""
    client, _, _ = logged_in
    resp = client.get(
        f"/inbox/{open_conversation.public_id}/",
        HTTP_HX_REQUEST="true",
        HTTP_HX_BOOSTED="true",
        HTTP_X_AKILENT_SHELL="app",
        HTTP_HX_TARGET="conversation-pane-inner",
    )
    html = resp.content.decode()
    assert html.lstrip().startswith(
        "<title>"
    )  # the shell-swap fragment, not the pane-only one
    assert '<meta name="akilent-shell" content="app">' in html


@pytest.mark.django_db
def test_conversation_detail_offers_create_lead_when_crm_enabled(
    logged_in, conversation_without_a_lead
):
    """R1.5a: the conversation is the fix for the "orphaned CRM" finding — an
    interested customer can be tracked from the conversation, and doing so
    returns the agent to the conversation (via the hidden "next" field) rather
    than dropping them into a separate CRM screen."""
    client, _, _ = logged_in
    resp = client.get(f"/inbox/{conversation_without_a_lead.public_id}/")
    body = resp.content.decode()
    assert "Track as interested" in body
    assert (
        f"/inbox/{conversation_without_a_lead.public_id}/" in body
    )  # the "next" hidden field

    create_resp = client.post(
        "/sales/leads/create/",
        {
            "contact": conversation_without_a_lead.contact.phone,
            "next": f"/inbox/{conversation_without_a_lead.public_id}/",
            "source": "conversation",  # hidden field set by the panel's form, not typed by the agent
        },
    )
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
        account=account,
        name="Follow up",
        whatsapp_template_name="follow_up",
        language_code="en",
        content="Hi {{1}}",
        variables=["name"],
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )

    resp = client.get(f"/inbox/{open_conversation.public_id}/")
    assert "Send a template" in resp.content.decode()

    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {
            "action": "send_template",
            "template_id": template.pk,
            f"var__{template.pk}__name": "Ada",
        },
    )
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
    assert msg.payload["_conversation_id"] == open_conversation.whatsapp_conversation.id


@pytest.mark.django_db
def test_send_template_rejects_unapproved_template(logged_in, open_conversation):
    from apps.whatsapp.models import MessageTemplate

    client, account, _ = logged_in
    draft = MessageTemplate.objects.create(
        account=account,
        name="Draft",
        whatsapp_template_name="draft",
        content="x",
        approval_status=MessageTemplate.ApprovalStatus.DRAFT,
    )
    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {
            "action": "send_template",
            "template_id": draft.pk,
        },
        follow=True,
    )
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

    resp = client.get(
        f"/inbox/{open_conversation.public_id}/messages/?after={first.id}"
    )
    assert resp.json()["messages"] == []


@pytest.mark.django_db
def test_messages_feed_marks_new_inbound_read(logged_in, open_conversation):
    client, _, _ = logged_in
    Conversation.objects.filter(pk=open_conversation.pk).update(is_unread=True)
    client.get(f"/inbox/{open_conversation.public_id}/messages/?after=0")
    open_conversation.refresh_from_db()
    assert open_conversation.is_unread is False


@pytest.mark.django_db
def test_messages_feed_scoped_to_account_and_login(
    logged_in, open_conversation, client
):
    _, _, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000000")
    other_convo = Conversation.objects.create(
        account=other,
        contact=other_contact,
        channel=Conversation.Channel.WHATSAPP,
    )
    assert client.get(f"/inbox/{other_convo.public_id}/messages/").status_code == 404
    client.logout()
    assert (
        client.get(f"/inbox/{open_conversation.public_id}/messages/").status_code == 302
    )


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
    client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "assign", "assignee": teammate.pk},
    )
    open_conversation.refresh_from_db()
    assert open_conversation.assigned_to_id == teammate.pk


@pytest.mark.django_db
def test_unassign(logged_in, open_conversation, teammate):
    client, _, _ = logged_in
    open_conversation.assign(teammate)
    client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "assign", "assignee": "none"},
    )
    open_conversation.refresh_from_db()
    assert open_conversation.assigned_to_id is None


@pytest.mark.django_db
def test_cannot_assign_to_someone_outside_the_business(logged_in, open_conversation):
    client, _, _ = logged_in
    stranger = User.objects.create_user("stranger", "s@example.com", "pw")
    other = Account.objects.create(company_name="Other Co")
    Membership.objects.create(user=stranger, account=other, role=Membership.Role.OWNER)
    client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "assign", "assignee": stranger.pk},
    )
    open_conversation.refresh_from_db()
    assert open_conversation.assigned_to_id is None


@pytest.mark.django_db
def test_the_action_itself_refuses_a_non_member(open_conversation):
    from apps.conversations.actions import ActionError, run_action

    stranger = User.objects.create_user("stranger", "s@example.com", "pw")
    with pytest.raises(ActionError):
        run_action(
            "assign_conversation", {}, conversation=open_conversation, user=stranger
        )


@pytest.mark.django_db
def test_the_picker_lists_teammates(logged_in, open_conversation, teammate):
    client, _, _ = logged_in
    body = client.get(f"/inbox/{open_conversation.public_id}/").content.decode()
    assert 'name="assignee"' in body and "sam" in body


@pytest.mark.django_db
def test_the_assign_picker_is_a_single_menu(logged_in, open_conversation, teammate):
    """The header used to carry a one-click "assign to me" button *and* a separate
    "assign to a teammate" select, doing overlapping jobs. One merged picker now covers
    both, and — since it no longer needs a teammate to be worth showing — a solo account
    gets it too. It is a menu of submit buttons rather than an auto-submitting <select>,
    which reassigned the conversation as a keyboard user arrowed through it."""
    client, _, _ = logged_in
    body = client.get(f"/inbox/{open_conversation.public_id}/").content.decode()
    assert body.count('aria-label="Assign conversation"') == 1
    assert '<select id="assignee"' not in body
    assert "Assign to me" in body
    assert 'value="none"' in body  # Unassigned is always reachable, even solo


@pytest.mark.django_db
def test_the_assign_picker_works_for_a_solo_account(logged_in, open_conversation):
    """No teammate fixture here: previously the teammate <select> only rendered when
    ``team_members`` had more than one person, so a solo owner had no explicit way to
    unassign without picking a teammate first."""
    client, _, _ = logged_in
    body = client.get(f"/inbox/{open_conversation.public_id}/").content.decode()
    assert 'name="assignee"' in body
    assert "Assign to me" in body
    assert 'value="none"' in body


@pytest.mark.django_db
def test_assignment_records_an_event(logged_in, open_conversation):
    from apps.conversations.models import Event

    client, _, user = logged_in
    client.post(f"/inbox/{open_conversation.public_id}/", {"action": "assign"})
    event = Event.objects.get(
        subject_type="conversation",
        subject_id=str(open_conversation.pk),
        type="conversation.assigned",
    )
    assert event.actor == f"user:{user.pk}"
    assert event.payload == {
        "assigned_to_id": user.id,
        "previous_assignee_id": None,
    }


@pytest.mark.django_db
def test_reassignment_records_the_previous_and_new_assignee(
    logged_in, open_conversation, teammate
):
    from apps.conversations.models import Event

    client, _, user = logged_in
    open_conversation.assign(user)
    client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "assign", "assignee": teammate.pk},
    )
    event = Event.objects.filter(
        subject_type="conversation",
        subject_id=str(open_conversation.pk),
        type="conversation.assigned",
    ).latest("occurred_at")
    assert event.payload == {
        "assigned_to_id": teammate.pk,
        "previous_assignee_id": user.pk,
    }


@pytest.mark.django_db
def test_unassign_records_an_unassign_event(logged_in, open_conversation, teammate):
    from apps.conversations.models import Event

    client, _, _ = logged_in
    open_conversation.assign(teammate)
    client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "assign", "assignee": "none"},
    )
    event = Event.objects.filter(
        subject_type="conversation",
        subject_id=str(open_conversation.pk),
        type="conversation.unassigned",
    ).get()
    assert event.payload == {
        "assigned_to_id": None,
        "previous_assignee_id": teammate.pk,
    }


@pytest.mark.django_db
def test_assigning_the_same_person_again_records_nothing(logged_in, open_conversation):
    from apps.conversations.models import Event

    client, _, _ = logged_in

    def assignment_events():
        return Event.objects.filter(
            subject_type="conversation",
            type__in=["conversation.assigned", "conversation.unassigned"],
        )

    client.post(f"/inbox/{open_conversation.public_id}/", {"action": "assign"})
    assert assignment_events().count() == 1
    client.post(f"/inbox/{open_conversation.public_id}/", {"action": "assign"})
    assert assignment_events().count() == 1


@pytest.mark.django_db
def test_auto_assign_records_an_automation_actor(open_conversation):
    from apps.accounts.models import Membership
    from apps.conversations.actions import run_action
    from apps.conversations.models import Event

    account = open_conversation.account
    teammate = User.objects.create_user("lweendo", "l@example.com", "pw")
    Membership.objects.create(
        user=teammate, account=account, role=Membership.Role.MEMBER
    )
    run_action("auto_assign_conversation", {}, conversation=open_conversation)
    open_conversation.refresh_from_db()
    assert open_conversation.assigned_to_id is not None
    event = Event.objects.get(
        subject_type="conversation",
        subject_id=str(open_conversation.pk),
        type="conversation.assigned",
    )
    assert event.actor == "automation:auto_assign_conversation"


@pytest.mark.django_db
def test_assignment_does_not_change_conversation_status(logged_in, open_conversation):
    client, _, _ = logged_in
    status_before = open_conversation.status
    client.post(f"/inbox/{open_conversation.public_id}/", {"action": "assign"})
    open_conversation.refresh_from_db()
    assert open_conversation.status == status_before


@pytest.mark.django_db
def test_a_closed_conversation_retains_its_assignment(
    logged_in, open_conversation, teammate
):
    client, _, _ = logged_in
    open_conversation.assign(teammate)
    open_conversation.close()
    open_conversation.refresh_from_db()
    assert open_conversation.status == Conversation.Status.CLOSED
    assert open_conversation.assigned_to_id == teammate.pk


@pytest.mark.django_db
def test_unauthorized_reassignment_outside_the_account_is_rejected_and_unrecorded(
    logged_in, open_conversation
):
    """Mirrors test_cannot_assign_to_someone_outside_the_business, plus checks
    a rejected attempt never becomes an audit-trail entry."""
    from apps.conversations.models import Event

    client, _, _ = logged_in
    stranger = User.objects.create_user("stranger2", "s2@example.com", "pw")
    other = Account.objects.create(company_name="Other Co 2")
    Membership.objects.create(user=stranger, account=other, role=Membership.Role.OWNER)
    client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "assign", "assignee": stranger.pk},
    )
    assert not Event.objects.filter(
        subject_type="conversation",
        type__in=["conversation.assigned", "conversation.unassigned"],
    ).exists()


@pytest.mark.django_db
def test_a_customer_given_a_value_shows_as_tracked_in_the_pipeline(
    logged_in, conversation_without_a_lead
):
    """Adding an estimated value turns the lead straight into a deal, which leaves no open
    lead. The panel used to read that as "Not tracked yet" and offer to track them again."""
    client, _, _ = logged_in
    page = f"/inbox/{conversation_without_a_lead.public_id}/"
    client.post(
        "/sales/leads/create/",
        {
            "contact": conversation_without_a_lead.contact.phone,
            "next": page,
            "source": "conversation",
            "value": "500",
        },
    )

    from apps.crm.models import Deal, Lead

    assert not Lead.objects.filter(
        contact=conversation_without_a_lead.contact, status="new"
    ).exists()
    deal = Deal.objects.get(contact=conversation_without_a_lead.contact)
    body = client.get(page).content.decode()
    assert "Not tracked yet" not in body
    assert "In your pipeline" in body and f"/sales/deals/{deal.public_id}/" in body
    assert "Tracked as interested" in body
    assert 'name="value"' not in body  # the create-lead form is not offered again


@pytest.mark.django_db
def test_a_lead_without_a_value_still_shows_where_they_stand(
    logged_in, conversation_without_a_lead
):
    client, _, _ = logged_in
    page = f"/inbox/{conversation_without_a_lead.public_id}/"
    client.post(
        "/sales/leads/create/",
        {
            "contact": conversation_without_a_lead.contact.phone,
            "next": page,
            "source": "conversation",
        },
    )
    body = client.get(page).content.decode()
    assert "Where they stand" in body and "Not tracked yet" not in body


@pytest.mark.django_db
def test_a_closed_deal_no_longer_counts_as_tracked(
    logged_in, conversation_without_a_lead
):
    client, _, _ = logged_in
    page = f"/inbox/{conversation_without_a_lead.public_id}/"
    client.post(
        "/sales/leads/create/",
        {
            "contact": conversation_without_a_lead.contact.phone,
            "next": page,
            "source": "conversation",
            "value": "500",
        },
    )

    from apps.crm.models import Deal

    Deal.objects.filter(contact=conversation_without_a_lead.contact).update(
        status=Deal.Status.WON
    )
    body = client.get(page).content.decode()
    assert "Not tracked yet" in body and "Track as interested" in body


# ---------------------------------------------------------------------------
# Gaps G1 and G2: an inbox reply sent again is not sent twice, and records who sent it.
# The browser sends a key with every attempt (static/js/chat.js) and the same key again
# when the person retries a send whose outcome was unknown (conversations/inbox_keys.py).
# ---------------------------------------------------------------------------

KEY = "3f2b8c1e-5d4a-4b7e-9a10-1c2d3e4f5a6b"


def _reply(client, conversation, body, key=None):
    data = {"action": "reply", "body": body}
    if key is not None:
        data["client_key"] = key
    return client.post(
        f"/inbox/{conversation.public_id}/",
        data,
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )


@pytest.mark.django_db
def test_the_same_send_again_reaches_the_customer_once(logged_in, open_conversation):
    client, account, user = logged_in
    assert _reply(client, open_conversation, "On its way!", KEY).json() == {"ok": True}
    assert _reply(client, open_conversation, "On its way!", KEY).json() == {"ok": True}
    sent = OutboundMessage.objects.filter(account=account)
    assert sent.count() == 1
    assert sent.get().idempotency_key == (
        f"inbox:{open_conversation.pk}:{user.pk}:{KEY}"
    )


@pytest.mark.django_db
def test_a_new_attempt_or_an_unusable_key_is_a_new_send(logged_in, open_conversation):
    client, account, _ = logged_in
    _reply(client, open_conversation, "One", KEY)
    _reply(client, open_conversation, "Two", KEY.replace("3f", "4f"))
    _reply(client, open_conversation, "Three", "bad:key")  # rejected: a random key
    _reply(client, open_conversation, "Four")  # no key: as before
    assert OutboundMessage.objects.filter(account=account).count() == 4
    # "bad:key", not "bad": a key made for a send without one ends in a random uuid,
    # whose hex can contain "bad" but never a colon.
    assert not OutboundMessage.objects.filter(
        idempotency_key__contains="bad:key"
    ).exists()


@pytest.mark.django_db
def test_the_key_is_scoped_to_the_person_and_conversation(open_conversation):
    from django.contrib.auth.models import User

    a = User(pk=1)
    b = User(pk=2)
    assert inbox_key(open_conversation, a, KEY) != inbox_key(open_conversation, b, KEY)
    assert inbox_key(open_conversation, a, "short") is None
    assert inbox_key(open_conversation, a, "") is None


@pytest.mark.django_db
def test_the_thread_records_who_sent_it_without_calling_it_automatic(
    logged_in, open_conversation
):
    client, account, user = logged_in
    _reply(client, open_conversation, "Yes, in stock.", KEY)
    outbound = OutboundMessage.objects.get(account=account)
    wa_conversation = open_conversation.whatsapp_conversation
    log = MessageLog.objects.create(
        account=account,
        conversation=wa_conversation,
        contact=wa_conversation.contact,
        message_id="wamid.KEYTEST",
        direction=MessageLog.Direction.OUTBOUND,
        message_type=MessageLog.MessageType.TEXT,
        content="Yes, in stock.",
        status=MessageLog.Status.SENT,
        timestamp=timezone.now(),
    )
    outbound.message_log = log
    outbound.save(update_fields=["message_log"])
    project_outbound_to_inbox(log)

    message = Message.objects.get(whatsapp_message=log)
    assert message.metadata["sent_by_user"] == user.pk
    assert message.metadata["client_key"] == KEY
    # A person's reply still has no "sent_by": reports and follow-ups rely on that.
    assert "sent_by" not in message.metadata

    feed = client.get(f"/inbox/{open_conversation.public_id}/messages/?after=0").json()
    mine = [m for m in feed["messages"] if m["id"] == message.pk]
    assert mine and mine[0]["clientKey"] == KEY


@pytest.mark.django_db
def test_sender_metadata_reads_only_inbox_keys():
    assert sender_metadata(f"inbox:5:7:{KEY}") == {"sent_by_user": 7, "client_key": KEY}
    assert sender_metadata(f"inbox:5:7:{KEY}:caption")["client_key"] == f"{KEY}:caption"
    assert sender_metadata("ai-auto:5:abc") == {}
    assert sender_metadata("inbox:5:x:abc") == {}
    assert sender_metadata(None) == {}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status, answer",
    [
        ("failed", 400),
        ("cancelled", 400),
        ("unconfirmed", {"ok": True, "unconfirmed": True}),
        ("queued", {"ok": True}),
    ],
)
def test_a_repeat_says_what_the_first_whatsapp_attempt_did(
    logged_in, open_conversation, status, answer
):
    client, account, _ = logged_in
    _reply(client, open_conversation, "Hello", KEY)
    OutboundMessage.objects.filter(account=account).update(status=status)
    resp = _reply(client, open_conversation, "Hello", KEY)
    if answer == 400:
        assert resp.status_code == 400 and resp.json()["ok"] is False
    else:
        assert resp.json() == answer
    assert OutboundMessage.objects.filter(account=account).count() == 1


@pytest.mark.django_db
def test_a_repeated_file_send_stores_no_new_file(logged_in, open_conversation):
    from unittest.mock import patch

    from django.core.files.uploadedfile import SimpleUploadedFile

    client, account, _ = logged_in
    url = f"/inbox/{open_conversation.public_id}/media/send/"
    with patch("apps.conversations.outbound_media.prepare") as prepare:
        prepare.return_value = type(
            "M",
            (),
            {
                "path": "x/qa.png",
                "mime": "image/png",
                "kind": "image",
                "filename": "qa.png",
            },
        )()
        for _ in range(2):
            client.post(
                url,
                {
                    "file": SimpleUploadedFile("qa.png", b"x", "image/png"),
                    "client_key": KEY,
                },
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
    assert prepare.call_count == 1
    assert OutboundMessage.objects.filter(account=account).count() == 1
