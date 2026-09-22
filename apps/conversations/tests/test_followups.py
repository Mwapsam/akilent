import pytest
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, FollowUp
from django.contrib.auth.models import User


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.fixture
def conversation(logged_in):
    _, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567")
    return Conversation.objects.create(account=account, contact=contact, channel=Conversation.Channel.WHATSAPP)


@pytest.mark.django_db
def test_create_followup_from_conversation_1h(logged_in, conversation):
    client, account, user = logged_in
    resp = client.post(f"/inbox/{conversation.public_id}/", {
        "action": "create_followup", "when": "1h", "note": "Check on order",
    })
    assert resp.status_code == 302
    followup = FollowUp.objects.get(account=account, conversation=conversation)
    assert followup.note == "Check on order"
    assert followup.created_by == user
    assert followup.due_at > timezone.now()


@pytest.mark.django_db
def test_create_followup_requires_valid_choice(logged_in, conversation):
    client, _, _ = logged_in
    resp = client.post(f"/inbox/{conversation.public_id}/", {"action": "create_followup", "when": ""}, follow=True)
    assert resp.status_code == 200
    assert FollowUp.objects.count() == 0


@pytest.mark.django_db
def test_followups_due_lists_only_due_and_open(logged_in, conversation):
    client, account, user = logged_in
    now = timezone.now()
    due = FollowUp.objects.create(
        account=account, contact=conversation.contact, conversation=conversation,
        due_at=now - timezone.timedelta(minutes=5),
    )
    FollowUp.objects.create(
        account=account, contact=conversation.contact, conversation=conversation,
        due_at=now + timezone.timedelta(days=1),
    )
    done = FollowUp.objects.create(
        account=account, contact=conversation.contact, conversation=conversation,
        due_at=now - timezone.timedelta(hours=1),
    )
    done.mark_done()

    resp = client.get("/inbox/followups/")
    assert resp.status_code == 200
    body = resp.content.decode()
    due_ids = [f.pk for f in resp.context["due"]]
    assert due_ids == [due.pk]
    assert len(resp.context["upcoming"]) == 1


@pytest.mark.django_db
def test_followup_complete_marks_done(logged_in, conversation):
    client, account, _ = logged_in
    followup = FollowUp.objects.create(
        account=account, contact=conversation.contact, conversation=conversation,
        due_at=timezone.now(),
    )
    resp = client.post(f"/inbox/followups/{followup.pk}/complete/")
    assert resp.status_code == 302
    followup.refresh_from_db()
    assert followup.done_at is not None


@pytest.mark.django_db
def test_followups_scoped_to_account(logged_in, conversation):
    client, account, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000000")
    other_followup = FollowUp.objects.create(account=other, contact=other_contact, due_at=timezone.now())

    resp = client.post(f"/inbox/followups/{other_followup.pk}/complete/")
    assert resp.status_code == 404
