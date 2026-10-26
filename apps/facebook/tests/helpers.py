"""Shared helpers for the Facebook inbox tests: a connected Page and a fake Graph API."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

APP_SECRET = "fb-test-secret"


def make_page(*, page_id=None, inbox=True):
    """An account with a connected, subscribed Page (and the ``facebook`` feature)."""
    from apps.billing.models import AccountFeatureOverride
    from apps.instagram.tests.helpers import make_account
    from apps.publishing.models import FacebookPageConnection

    account, user = make_account()
    conn = FacebookPageConnection.objects.create(
        account=account,
        page_id=page_id or f"page{secrets.token_hex(4)}",
        page_name="Test Page",
        access_token="EAA-page-token",
        status=FacebookPageConnection.Status.ACTIVE,
        webhook_subscribed_at=timezone.now(),
    )
    if inbox:
        AccountFeatureOverride.objects.create(
            account=account, key="facebook", grant=True
        )
    return account, user, conn


def make_messenger_contact(conn, *, psid=None, last_inbound=timedelta(minutes=5)):
    """A Messenger customer whose last message was ``last_inbound`` ago."""
    from apps.facebook.services.contacts import resolve_or_create_contact
    from apps.facebook.services.conversations import (
        get_or_create_facebook_conversation,
    )

    with patch("apps.facebook.services.contacts._enrich_contact_profile"):
        fb_contact = resolve_or_create_contact(
            conn, psid or f"psid{secrets.token_hex(4)}", name="Ada Lovelace"
        )
    fb_convo, spine = get_or_create_facebook_conversation(fb_contact, conn.page_id)
    if last_inbound is not None:
        fb_convo.last_inbound_at = timezone.now() - last_inbound
        fb_convo.save(update_fields=["last_inbound_at"])
    return fb_contact, fb_convo, spine


def dm_payload(page_id, psid, text, *, mid=None, echo=False, attachments=None):
    ts = int(timezone.now().timestamp() * 1000)
    message = {"mid": mid or f"m_{secrets.token_hex(8)}", "text": text}
    if echo:
        message["is_echo"] = True
    if attachments:
        message["attachments"] = attachments
    sender, recipient = (page_id, psid) if echo else (psid, page_id)
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": ts,
                "messaging": [
                    {
                        "sender": {"id": sender},
                        "recipient": {"id": recipient},
                        "timestamp": ts,
                        "message": message,
                    }
                ],
            }
        ],
    }


def comment_payload(
    page_id,
    commenter_id,
    text,
    *,
    comment_id=None,
    post_id=None,
    parent_id=None,
    verb="add",
    name="Grace Hopper",
):
    post_id = post_id or f"{page_id}_{secrets.token_hex(4)}"
    comment_id = comment_id or f"{post_id}_{secrets.token_hex(4)}"
    return {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": int(timezone.now().timestamp()),
                "changes": [
                    {
                        "field": "feed",
                        "value": {
                            "item": "comment",
                            "verb": verb,
                            "comment_id": comment_id,
                            "post_id": post_id,
                            "parent_id": parent_id or post_id,
                            "from": {"id": commenter_id, "name": name},
                            "message": text,
                            "created_time": int(timezone.now().timestamp()),
                        },
                    }
                ],
            }
        ],
    }


def post_webhook(client, payload: dict):
    body = json.dumps(payload).encode()
    signature = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    from django.test import TestCase

    # The view enqueues processing on commit; run it, as the worker would.
    with TestCase.captureOnCommitCallbacks(execute=True):
        return client.post(
            "/facebook/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=f"sha256={signature}",
        )


class FakeGraph:
    """Stands in for ``requests.request`` in apps.facebook.providers.meta.

    Records every call as ``(method, path, json, params)``; ``fail`` makes matching
    paths answer with a Graph error.
    """

    def __init__(self, *, recipient_psid="", fail: dict | None = None):
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.recipient_psid = recipient_psid
        self.fail = fail or {}

    def __call__(self, method, url, json=None, params=None, timeout=None):
        path = url.split(".com/", 1)[1].split("/", 1)[1]
        self.calls.append((method, path, json or {}, params or {}))
        for fragment, (code, message) in self.fail.items():
            if fragment in path:
                return _Resp(400, {"error": {"code": code, "message": message}})
        if path.endswith("/messages"):
            data = {"message_id": f"m_{secrets.token_hex(6)}"}
            if self.recipient_psid:
                data["recipient_id"] = self.recipient_psid
            return _Resp(200, data)
        if path.endswith("/comments"):
            return _Resp(200, {"id": f"c_{secrets.token_hex(6)}"})
        if method == "get":
            return _Resp(
                200, {"name": "Profile Name", "profile_pic": "https://x/p.jpg"}
            )
        return _Resp(200, {"success": True})

    def sent(self, suffix="/messages"):
        return [c for c in self.calls if c[0] == "post" and c[1].endswith(suffix)]


class _Resp:
    def __init__(self, status, data):
        self.status_code = status
        self.ok = status < 400
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data
