"""Attachment parsing/validation + provider wiring."""
import base64
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailDomain, EmailMessage
from apps.email.services.attachments import AttachmentError, parse_attachments
from apps.email.services.mime import build_mime
from apps.email.types import Attachment, OutboundEmail


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_parse_attachments_decodes_and_reports_metadata():
    atts = parse_attachments([
        {"filename": "a.txt", "content_b64": _b64(b"hello"), "content_type": "text/plain"},
    ])
    assert len(atts) == 1
    assert atts[0].filename == "a.txt"
    assert atts[0].content == b"hello"


def test_parse_attachments_rejects_bad_input():
    with pytest.raises(AttachmentError):
        parse_attachments([{"content_b64": _b64(b"x")}])  # no filename
    with pytest.raises(AttachmentError):
        parse_attachments([{"filename": "a", "content_b64": "not base64!!!"}])
    with pytest.raises(AttachmentError):
        parse_attachments([{"filename": "big", "content_b64": _b64(b"x" * (5 * 1024 * 1024 + 1))}])


def test_build_mime_includes_attachment():
    raw = build_mime(OutboundEmail(
        from_email="a@x.com", to_email="b@y.com", subject="hi",
        text_body="body", html_body="<p>body</p>",
        attachments=(Attachment("invoice.pdf", b"%PDF-1.4 ...", "application/pdf"),),
    ))
    assert b"invoice.pdf" in raw
    assert b"application/pdf" in raw


@pytest.fixture
def api_key(db):
    user = User.objects.create_user("o", "o@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"),
                               max_emails_per_month=100, email_apis=True, api_rate_per_min=0)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    EmailDomain.objects.create(account=acc, domain="mail.acme.com",
                               status=EmailDomain.Status.VERIFIED)
    k, raw = EmailApiKey.create_for_account(acc, name="k")
    return raw, acc


@pytest.mark.django_db
def test_send_with_attachment_stores_metadata_and_passes_bytes(client, api_key, django_capture_on_commit_callbacks):
    key, acc = api_key
    payload = {
        "from": "hi@mail.acme.com", "to": "user@example.com",
        "subject": "Here is your file", "text": "see attached",
        "attachments": [
            {"filename": "report.csv", "content_b64": _b64(b"a,b\n1,2\n"), "content_type": "text/csv"},
        ],
    }
    with patch("apps.email.tasks.send_email.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            r = client.post("/api/v1/messages", data=payload, content_type="application/json",
                            HTTP_X_API_KEY=key)
    assert r.status_code == 202
    msg = EmailMessage.objects.get(pk=r.json()["id"])
    assert msg.attachments == [{"filename": "report.csv", "content_type": "text/csv", "size": 8}]
    kwargs = delay.call_args.kwargs
    assert kwargs["attachments"][0]["filename"] == "report.csv"
    assert kwargs["attachments"][0]["content_b64"] == _b64(b"a,b\n1,2\n")


@pytest.mark.django_db
def test_send_with_invalid_attachment_returns_400(client, api_key):
    key, _ = api_key
    r = client.post("/api/v1/messages", data={
        "from": "hi@mail.acme.com", "to": "user@example.com", "text": "x",
        "attachments": [{"filename": "bad", "content_b64": "%%%"}],
    }, content_type="application/json", HTTP_X_API_KEY=key)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_attachment"
