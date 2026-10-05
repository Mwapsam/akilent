"""
Shared test helpers for Instagram Phase 1 tests.
"""
import hashlib
import hmac
import json
import secrets
from io import BytesIO

from django.contrib.auth.models import User
from django.http import HttpRequest
from django.utils import timezone


# ---------------------------------------------------------------------------
# Account / user fixtures
# ---------------------------------------------------------------------------


def make_account(slug=None):
    from apps.accounts.models import Account, Membership

    slug = slug or secrets.token_hex(4)
    user = User.objects.create_user(
        username=f"{slug}@test.com",
        email=f"{slug}@test.com",
        password="x",
    )
    account = Account.objects.create(
        company_name=f"Test Co {slug}",
        slug=slug,
        selected_services="instagram",
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account, user


def make_instagram_account(account, *, ibaid=None, page_id=None, verify_token="test_verify"):
    from apps.instagram.models.account import InstagramBusinessAccount

    ibaid = ibaid or f"ig_{secrets.token_hex(6)}"
    page_id = page_id or f"page_{secrets.token_hex(6)}"
    return InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id=ibaid,
        page_id=page_id,
        access_token="test_token",
        verify_token=verify_token,
        is_active=True,
    )


def make_instagram_contact(account, ig_account, *, igsid=None):
    from apps.instagram.models.contact import InstagramContact
    from apps.contacts.models import Contact

    igsid = igsid or f"igsid_{secrets.token_hex(6)}"
    contact = Contact.objects.create(account=account, source="instagram")
    ig_contact = InstagramContact.objects.create(
        account=account,
        instagram_scoped_id=igsid,
        contact=contact,
    )
    return ig_contact, contact


# ---------------------------------------------------------------------------
# Webhook payload builders
# ---------------------------------------------------------------------------


def dm_payload(page_id: str, sender_igsid: str, body: str, *, msg_id=None, ts=None) -> dict:
    ts = ts or int(timezone.now().timestamp() * 1000)
    msg_id = msg_id or f"mid.{secrets.token_hex(8)}"
    return {
        "object": "instagram",
        "entry": [
            {
                "id": page_id,
                "time": int(ts / 1000),
                "messaging": [
                    {
                        "sender": {"id": sender_igsid},
                        "recipient": {"id": page_id},
                        "timestamp": ts,
                        "message": {"mid": msg_id, "text": body},
                    }
                ],
            }
        ],
    }


def comment_payload(page_id: str, sender_igsid: str, body: str, *, comment_id=None, post_id=None) -> dict:
    comment_id = comment_id or f"cmt_{secrets.token_hex(6)}"
    post_id = post_id or f"post_{secrets.token_hex(6)}"
    return {
        "object": "instagram",
        "entry": [
            {
                "id": page_id,
                "time": int(timezone.now().timestamp()),
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "id": comment_id,
                            "text": body,
                            "from": {"id": sender_igsid, "username": "tester"},
                            "media": {"id": post_id},
                            "timestamp": timezone.now().isoformat(),
                        },
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Signed HTTP request helper
# ---------------------------------------------------------------------------


def signed_post(payload: dict, secret: str = "test_token") -> HttpRequest:
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = HttpRequest()
    request.method = "POST"
    request._stream = BytesIO(body)
    request._body = body
    request.META = {
        "CONTENT_TYPE": "application/json",
        "HTTP_X_HUB_SIGNATURE_256": f"sha256={sig}",
        "REMOTE_ADDR": "127.0.0.1",
    }
    return request
