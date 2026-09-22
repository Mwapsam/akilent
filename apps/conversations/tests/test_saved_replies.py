import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, SavedReply


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.mark.django_db
def test_create_saved_reply(logged_in):
    client, account, _ = logged_in
    resp = client.post("/inbox/saved-replies/", {"title": "Store hours", "body": "Open 9-6 Mon-Sat."})
    assert resp.status_code == 302
    assert SavedReply.objects.filter(account=account, title="Store hours").exists()


@pytest.mark.django_db
def test_create_saved_reply_requires_both_fields(logged_in):
    client, account, _ = logged_in
    client.post("/inbox/saved-replies/", {"title": "", "body": "x"})
    client.post("/inbox/saved-replies/", {"title": "x", "body": ""})
    assert SavedReply.objects.filter(account=account).count() == 0


@pytest.mark.django_db
def test_saved_replies_scoped_to_account(logged_in):
    client, account, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_reply = SavedReply.objects.create(account=other, title="Other", body="x")

    resp = client.get("/inbox/saved-replies/")
    assert other_reply.title not in resp.content.decode()

    del_resp = client.post(f"/inbox/saved-replies/{other_reply.pk}/delete/")
    assert del_resp.status_code == 404
    assert SavedReply.objects.filter(pk=other_reply.pk).exists()


@pytest.mark.django_db
def test_delete_saved_reply(logged_in):
    client, account, _ = logged_in
    reply = SavedReply.objects.create(account=account, title="Store hours", body="Open 9-6.")
    resp = client.post(f"/inbox/saved-replies/{reply.pk}/delete/")
    assert resp.status_code == 302
    assert not SavedReply.objects.filter(pk=reply.pk).exists()


@pytest.mark.django_db
def test_composer_shows_saved_reply_picker(logged_in):
    client, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567")
    conversation = Conversation.objects.create(account=account, contact=contact, channel=Conversation.Channel.WHATSAPP)
    SavedReply.objects.create(account=account, title="Store hours", body="Open 9-6.")

    resp = client.get(f"/inbox/{conversation.public_id}/")
    body = resp.content.decode()
    assert "Insert a saved reply" in body
    assert "Store hours" in body
