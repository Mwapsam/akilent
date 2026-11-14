"""Ad-burst load test: a Meta ad sends hundreds of chats in an hour.

docs/strategy/akilent-ads.md, "ad chats that run smoothly". Through the real signed webhook
endpoints for WhatsApp (Click-to-WhatsApp) and Instagram (Click-to-Instagram-Direct), with
the "Answer ad chats straight away" automation on and AI off. Only outbound network sends are
stubbed: the WhatsApp outbox drain, read receipts, and Instagram's Graph send.

Checks that every message is stored once, every chat gets exactly one ad referral, one ad
entry point and one automated reply, that replaying the whole burst changes nothing, and
that the ad verdict and the "ad chats waiting" sweep stay fast at that volume.

Timings are deliberately generous (like ``test_phase4_load.py``); a run that blows a budget
by more than 2x needs investigating. Sizes can be raised with ``AD_BURST_WA`` /
``AD_BURST_IG``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import timedelta
from itertools import count
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation.engagement_starters import (
    AD_REPLY_PRIORITY,
    STARTERS_BY_KEY,
    build_reply_definition,
)
from apps.automation.models import Workflow, WorkflowRun
from apps.conversations import ad_alerts, ad_verdict, reporting
from apps.conversations.models import (
    AdReferral,
    Conversation,
    ConversationEntryPoint,
    Message,
)
from apps.core import channels as ch

pytestmark = [pytest.mark.django_db, pytest.mark.performance]

WA_CHATS = int(os.environ.get("AD_BURST_WA", "500"))
IG_CHATS = int(os.environ.get("AD_BURST_IG", "300"))
SECRET = "test_app_secret"
# Per inbound message, end to end (webhook, inbox, ad capture, lead capture, automation,
# queued reply). Measured 2026-11-14 on a Windows laptop with SQLite: about 0.5-1 s, of which
# the ad capture and ad lookup are a few milliseconds; the rest is the existing inbound path.
# At 1.5 s one worker still takes 2,400 messages an hour, far above an ad burst.
SECONDS_PER_MESSAGE = 1.5
# Database queries for one ad message, end to end. Catches an N+1 creeping in.
MAX_QUERIES_PER_MESSAGE = 400

WA_AD = {
    "source_url": "https://fb.me/abc",
    "source_id": "120200000000001",
    "source_type": "ad",
    "headline": "Summer dresses",
    "body": "Tap to chat",
    "media_type": "image",
    "ctwa_clid": "ARAkLkA8rmlFeiCktEJQ",
}
IG_AD = {
    "ref": "",
    "source": "ADS",
    "type": "OPEN_THREAD",
    "ad_id": "120200000000002",
    "ads_context_data": {"ad_title": "New sandals", "photo_url": "https://x/p.jpg"},
}


@pytest.fixture(autouse=True)
def _setup(settings):
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    settings.FACEBOOK_ENABLED = True
    settings.WHATSAPP_APP_SECRET = SECRET
    settings.RELEASED_FEATURES = frozenset({"ads_automation", "ads_inbox_verdict"})
    ch.invalidate_channel_cache()
    # Automations start from customer messages (the site-wide switch, on in production).
    from django.core.cache import cache

    from apps.core.models import SiteSettings
    from apps.whatsapp.tasks import _AUTOMATION_EVENTS_CACHE_KEY

    site = SiteSettings.load()
    site.automation_events_enabled = True
    site.save()
    cache.delete(_AUTOMATION_EVENTS_CACHE_KEY)
    targets = (
        "apps.whatsapp.tasks.mark_read",
        "apps.whatsapp.tasks.drain_outbound_queue",
        "apps.whatsapp.tasks._auto_reply_during_setup",
    )
    patchers = [patch(t) for t in targets]
    # Instagram's Graph send: "not sent yet", so the reply stays queued in the outbox.
    patchers.append(
        patch("apps.instagram.services.outbound.send_outbound", return_value=False)
    )
    for p in patchers:
        p.start()
    yield
    for p in patchers:
        p.stop()
    ch.invalidate_channel_cache()


class Business:
    def __init__(self):
        from apps.instagram.models.account import InstagramBusinessAccount
        from apps.whatsapp.models import WhatsAppBusinessNumber

        self.account = Account.objects.create(company_name="Ad Shop", slug="ad-shop")
        self.owner = User.objects.create_user("owner", "owner@adshop.example", "x")
        Membership.objects.create(
            user=self.owner, account=self.account, role=Membership.Role.OWNER
        )
        WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="PNID",
            display_number="+260 97 1111111",
            waba_id="WABA",
            access_token="wa-token",
            registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
        )
        self.ig = InstagramBusinessAccount.objects.create(
            account=self.account,
            instagram_business_account_id="IGBA",
            page_id="IGPAGE",
            access_token="ig-token",
            verify_token="v",
            is_active=True,
        )
        starter = STARTERS_BY_KEY["answer-ad-chats"]
        self.workflow = Workflow.objects.create(
            account=self.account,
            name=starter["name"],
            slug=starter["key"],
            status=Workflow.Status.PUBLISHED,
            priority=AD_REPLY_PRIORITY,
            definition=build_reply_definition(
                starter, text="Thanks for messaging us about our ad!"
            ),
        )
        self._ids = count(1)

    # -- webhooks ---------------------------------------------------------------------------
    @staticmethod
    def _signed(url, payload):
        body = json.dumps(payload).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return Client().post(
            url,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=f"sha256={sig}",
        )

    def whatsapp_payload(self, n, *, referral=True):
        phone = f"26096{n:07d}"
        item = {
            "from": phone,
            "id": f"wamid.burst.{n}",
            "timestamp": str(int(time.time())),
            "type": "text",
            "text": {"body": "Hi, is this still available?"},
        }
        if referral:
            item["referral"] = WA_AD
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "260971111111",
                                    "phone_number_id": "PNID",
                                },
                                "contacts": [
                                    {"profile": {"name": f"C{n}"}, "wa_id": phone}
                                ],
                                "messages": [item],
                            },
                        }
                    ],
                }
            ],
        }

    def instagram_payload(self, n):
        ts = int(timezone.now().timestamp() * 1000)
        return {
            "object": "instagram",
            "entry": [
                {
                    "id": "IGBA",
                    "time": ts // 1000,
                    "messaging": [
                        {
                            "sender": {"id": f"igsid_burst_{n}"},
                            "recipient": {"id": "IGBA"},
                            "timestamp": ts,
                            "message": {
                                "mid": f"mid.burst.{n}",
                                "text": "Price?",
                                "referral": IG_AD,
                            },
                        }
                    ],
                }
            ],
        }

    def send_whatsapp(self, payload):
        return self._signed(reverse("whatsapp-webhook"), payload)

    def send_instagram(self, payload):
        return self._signed(reverse("instagram-webhook"), payload)


def _deliver_all(business, capture, wa_payloads, ig_payloads):
    with capture(execute=True):
        for payload in wa_payloads:
            assert business.send_whatsapp(payload).status_code == 200
        for payload in ig_payloads:
            assert business.send_instagram(payload).status_code == 200


def _snapshot(account):
    from apps.instagram.models.message import OutboundMessage
    from apps.whatsapp.models import OutboundMessage as WhatsAppOutbound

    return {
        "conversations": Conversation.objects.filter(account=account).count(),
        "inbound": Message.objects.filter(
            account=account, direction=Message.Direction.INBOUND
        ).count(),
        "referrals": AdReferral.objects.filter(account=account).count(),
        "ad_entries": ConversationEntryPoint.objects.filter(
            account=account, kind=ConversationEntryPoint.Kind.AD_REFERRAL
        ).count(),
        "runs": WorkflowRun.objects.filter(workflow__account=account).count(),
        # Queued replies: the outbox drain (stubbed) would send them.
        "wa_replies": WhatsAppOutbound.objects.filter(account=account).count(),
        "ig_replies": OutboundMessage.objects.filter(
            instagram_account__account=account
        ).count(),
    }


def test_an_ad_burst_is_stored_answered_once_and_replay_safe(
    django_capture_on_commit_callbacks,
):
    business = Business()
    wa = [business.whatsapp_payload(n) for n in range(WA_CHATS)]
    ig = [business.instagram_payload(n) for n in range(IG_CHATS)]
    total = WA_CHATS + IG_CHATS
    # One chat of each first, outside the clock: a worker imports its code once, not per chat.
    _deliver_all(
        business,
        django_capture_on_commit_callbacks,
        [business.whatsapp_payload(10**6)],
        [business.instagram_payload(10**6)],
    )
    before = _snapshot(business.account)

    started = time.monotonic()
    if os.environ.get("AD_BURST_PROFILE"):  # where the time goes: AD_BURST_PROFILE=<file>
        import cProfile

        with cProfile.Profile() as profile:
            _deliver_all(business, django_capture_on_commit_callbacks, wa, ig)
        profile.dump_stats(os.environ["AD_BURST_PROFILE"])
    else:
        _deliver_all(business, django_capture_on_commit_callbacks, wa, ig)
    elapsed = time.monotonic() - started

    after = _snapshot(business.account)
    assert {k: after[k] - before[k] for k in after} == {
        "conversations": total,
        "inbound": total,
        "referrals": total,
        "ad_entries": total,
        "runs": total,
        "wa_replies": WA_CHATS,
        "ig_replies": IG_CHATS,
    }
    # Every chat counts as paid, none as a post's.
    overall = reporting.last_days(1)
    from apps.publishing.results import journey

    assert journey(business.account, overall)["ad_chats"] == total + 2
    budget = total * SECONDS_PER_MESSAGE
    assert elapsed < budget, f"{total} ad messages took {elapsed:.1f}s (budget {budget:.0f}s)"

    # Meta redelivers the whole burst: nothing new is stored, nobody is answered twice.
    _deliver_all(business, django_capture_on_commit_callbacks, wa, ig)
    assert _snapshot(business.account) == after


def test_query_count_for_one_ad_message_is_bounded(django_capture_on_commit_callbacks):
    business = Business()
    # Warm up caches (channel settings, site settings) with one chat first.
    _deliver_all(
        business, django_capture_on_commit_callbacks, [business.whatsapp_payload(0)], []
    )
    with CaptureQueriesContext(connection) as queries:
        _deliver_all(
            business,
            django_capture_on_commit_callbacks,
            [business.whatsapp_payload(1)],
            [],
        )
    assert len(queries) <= MAX_QUERIES_PER_MESSAGE, (
        f"one ad message ran {len(queries)} queries"
    )


def _bulk_ad_chats(account, n, *, ago):
    """``n`` ad chats with their first message ``ago`` in the past, made directly in bulk."""
    from apps.contacts.models import Contact

    when = timezone.now() - ago
    contacts = Contact.objects.bulk_create(
        [Contact(account=account, phone=f"+26095{i:07d}") for i in range(n)]
    )
    conversations = Conversation.objects.bulk_create(
        [
            Conversation(
                account=account, contact=c, channel=Conversation.Channel.WHATSAPP
            )
            for c in contacts
        ]
    )
    messages = Message.objects.bulk_create(
        [
            Message(
                account=account,
                conversation=c,
                direction=Message.Direction.INBOUND,
                body="Hi",
                timestamp=when,
            )
            for c in conversations
        ]
    )
    AdReferral.objects.bulk_create(
        [
            AdReferral(
                account=account,
                conversation=c,
                message=m,
                channel=AdReferral.Channel.WHATSAPP,
                source_type="ad",
                source_id=f"ad{i % 20}",
                headline=f"Ad {i % 20}",
                received_at=when,
            )
            for i, (c, m) in enumerate(zip(conversations, messages, strict=True))
        ]
    )


def test_the_verdict_and_the_waiting_sweep_stay_fast_at_volume():
    business = Business()
    _bulk_ad_chats(business.account, ad_verdict.ROW_LIMIT, ago=timedelta(hours=2))

    started = time.monotonic()
    rows = ad_verdict.report(business.account, reporting.last_days(1))
    verdict_seconds = time.monotonic() - started
    assert sum(r["chats"] for r in rows) == ad_verdict.ROW_LIMIT
    assert verdict_seconds < 30, f"verdict on 2,000 ad chats took {verdict_seconds:.1f}s"

    sent = []
    with patch(
        "apps.accounts.notifications.notify_team",
        side_effect=lambda account, **kw: sent.append(account) or 1,
    ):
        started = time.monotonic()
        result = ad_alerts.sweep()
        sweep_seconds = time.monotonic() - started
    assert result == {"chats": ad_verdict.ROW_LIMIT, "emails": 1}
    assert sent == [business.account]  # one digest, not 2,000 emails
    assert sweep_seconds < 60, f"sweep over 2,000 ad chats took {sweep_seconds:.1f}s"
