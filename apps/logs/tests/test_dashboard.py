import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.email.models import EmailMessage
from apps.logs.models import ApiRequest
from apps.logs.services import record_message_event


@pytest.fixture
def user_account(db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return user, acc


@pytest.fixture
def logged_in(client, user_account):
    user, acc = user_account
    client.force_login(user)
    return client, acc


@pytest.mark.django_db
def test_message_list_and_detail_render(logged_in):
    client, acc = logged_in
    msg = EmailMessage.objects.create(
        account=acc, from_email="a@acme.test", to_email="b@example.com", subject="Hi"
    )
    record_message_event(msg, "queued", source="api")
    record_message_event(msg, "sent", source="pipeline")

    resp = client.get("/logs/messages/")
    assert resp.status_code == 200
    assert msg.public_id in resp.content.decode()

    detail = client.get(f"/logs/messages/{msg.public_id}/")
    assert detail.status_code == 200
    body = detail.content.decode()
    assert "Timeline" in body
    assert "Sent" in body


@pytest.mark.django_db
def test_message_detail_scoped_to_account(logged_in):
    client, _ = logged_in
    other = Account.objects.create(company_name="Other")
    msg = EmailMessage.objects.create(
        account=other, from_email="x@o.test", to_email="y@example.com"
    )
    assert client.get(f"/logs/messages/{msg.public_id}/").status_code == 404


@pytest.mark.django_db
def test_request_log_list_renders(logged_in):
    client, acc = logged_in
    ApiRequest.objects.create(
        account=acc, method="POST", path="/api/v1/messages", status_code=202, latency_ms=5
    )
    resp = client.get("/logs/requests/")
    assert resp.status_code == 200
    assert "/api/v1/messages" in resp.content.decode()
