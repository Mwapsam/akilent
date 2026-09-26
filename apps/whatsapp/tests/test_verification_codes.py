"""One-time codes sent by another system through the API, and the send-time copy-code button."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.email.models import EmailApiKey
from apps.whatsapp import verification_codes
from apps.whatsapp.models import MessageTemplate, OutboundMessage, WhatsAppContact
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.send_components import build_send_components
from apps.whatsapp.tasks import drain_outbound_queue
from apps.whatsapp.types import SendResult

URL = "/api/v1/whatsapp/verification-codes"
PHONE = "+260971234567"
_wa_urls = override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled", WHATSAPP_ENABLED=True)


class _FakeProvider:
    def __init__(self):
        self.calls = []

    def send_template(self, to, name, language, components):
        self.calls.append((to, name, language, components))
        return SendResult(message_id="wamid.CODE1", success=True)


@pytest.fixture(autouse=True)
def _no_auto_drain():
    # Celery runs eagerly in tests; each test drains explicitly.
    with patch("apps.whatsapp.tasks.drain_outbound_queue.delay"):
        yield


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    WhatsAppBusinessNumber.objects.create(account=acc, phone_number_id="PNID", waba_id="WABA1",
                                          access_token="tok", is_active=True)
    return acc


@pytest.fixture
def template(account):
    return MessageTemplate.objects.create(
        account=account, name="login_code", whatsapp_template_name="login_code", language_code="en",
        category=MessageTemplate.Category.AUTHENTICATION, approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        content="*{{1}}* is your verification code.", footer="This code expires in 5 minutes.",
        variables=["Verification code"], buttons=[{"type": "OTP", "otp_type": "COPY_CODE", "text": "Copy code"}],
    )


def _key(account, mode="live"):
    _, raw = EmailApiKey.create_for_account(account, name="default", mode=mode)
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _drain():
    provider = _FakeProvider()
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        drain_outbound_queue()
    return provider


def test_an_inbox_or_automation_send_fills_the_copy_code_button(account, template):
    components = build_send_components(template, {"Verification code": "482913"})
    assert components[1] == {"type": "button", "sub_type": "url", "index": "0",
                             "parameters": [{"type": "text", "text": "482913"}]}


@pytest.mark.django_db
def test_the_api_queues_a_code_and_whatsapp_gets_it_in_body_and_button(client, account, template):
    resp = client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json", **_key(account))
    assert resp.status_code == 202, resp.content
    body = resp.json()
    assert body["status"] == "queued" and body["template"] == "login_code" and body["to"] == PHONE

    provider = _drain()
    (to, name, language, components), = provider.calls
    assert (to, name, language) == (PHONE, "login_code", "en")
    assert components == verification_codes.components("482913")


@pytest.mark.django_db
def test_the_code_is_blanked_after_sending_and_stays_out_of_the_inbox(client, account, template):
    from apps.conversations.models import Message as SpineMessage

    resp = client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json", **_key(account))
    _drain()
    msg = OutboundMessage.objects.get(pk=resp.json()["id"])
    assert msg.status == OutboundMessage.Status.SENT
    assert "482913" not in str(msg.payload) and "482913" not in str(msg.message_log.raw_payload)
    assert not SpineMessage.objects.filter(body__icontains="login_code").exists()

    status = client.get(f"{URL}/{msg.pk}", **_key(account)).json()
    assert status["status"] == "sent" and status["sent_at"]


@pytest.mark.django_db
def test_a_code_that_waited_past_its_expiry_is_dropped_not_sent_late(client, account, template):
    resp = client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json", **_key(account))
    msg = OutboundMessage.objects.get(pk=resp.json()["id"])
    payload = dict(msg.payload, expires_at=(timezone.now() - timedelta(minutes=1)).isoformat())
    OutboundMessage.objects.filter(pk=msg.pk).update(payload=payload)

    assert _drain().calls == []
    msg.refresh_from_db()
    assert msg.status == OutboundMessage.Status.FAILED and "482913" not in str(msg.payload)
    status = client.get(f"{URL}/{msg.pk}", **_key(account)).json()
    assert status["error"]["code"] == "CODE_EXPIRED"


@pytest.mark.django_db
def test_an_earlier_stop_does_not_block_a_code_the_person_asked_for(client, account, template):
    WhatsAppContact.objects.create(account=account, phone_number=PHONE,
                                   opt_in_status=WhatsAppContact.OptInStatus.OPTED_OUT)
    client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json", **_key(account))
    assert len(_drain().calls) == 1


@pytest.mark.django_db
def test_the_same_idempotency_key_sends_once(client, account, template):
    headers = {**_key(account), "HTTP_IDEMPOTENCY_KEY": "login-1"}
    first = client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json", **headers)
    again = client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json", **headers)
    assert first.json()["id"] == again.json()["id"]
    assert OutboundMessage.objects.filter(account=account).count() == 1


@pytest.mark.django_db
def test_a_test_key_checks_but_sends_nothing(client, account, template):
    resp = client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json",
                       **_key(account, mode="test"))
    assert resp.status_code == 202 and resp.json()["status"] == "test"
    assert not OutboundMessage.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("data, status, code", [
    ({"to": "12", "code": "482913"}, 400, "invalid_phone"),
    ({"to": PHONE, "code": "48 29"}, 400, "invalid_code"),
    ({"to": PHONE, "code": "482913", "template": "nope"}, 404, "template_not_found"),
])
def test_bad_requests_say_what_is_wrong(client, account, template, data, status, code):
    resp = client.post(URL, data, content_type="application/json", **_key(account))
    assert resp.status_code == status and resp.json()["error"]["code"] == code


@pytest.mark.django_db
def test_without_an_approved_authentication_template_nothing_is_queued(client, account):
    resp = client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json", **_key(account))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "no_authentication_template"


@pytest.mark.django_db
def test_one_number_gets_at_most_five_codes_an_hour(client, account, template):
    for _ in range(verification_codes.MAX_PER_HOUR):
        assert client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json",
                           **_key(account)).status_code == 202
    resp = client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json", **_key(account))
    assert resp.status_code == 429 and resp.json()["error"]["code"] == "too_many_codes"


@pytest.mark.django_db
def test_another_business_cannot_read_the_status(client, account, template):
    resp = client.post(URL, {"to": PHONE, "code": "482913"}, content_type="application/json", **_key(account))
    other = Account.objects.create(company_name="Other")
    assert client.get(f"{URL}/{resp.json()['id']}", **_key(other)).status_code == 404


@_wa_urls
@pytest.mark.django_db
def test_the_page_lets_an_owner_create_a_key_and_shows_the_template(client, account, template):
    client.force_login(User.objects.get(username="owner"))
    html = client.get("/whatsapp/codes/").content.decode()
    assert "login_code" in html and "Create an API key" in html

    client.post("/whatsapp/codes/key/")
    html = client.get("/whatsapp/codes/").content.decode()
    assert "ak_live_" in html and EmailApiKey.objects.filter(account=account, is_active=True).count() == 1
