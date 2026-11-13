"""Facebook Page comments get audience signals on arrival and a new run on edit."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.audience import services
from apps.audience.models import AudienceSignalRun
from apps.facebook.models import FacebookComment
from apps.facebook.tests.helpers import (
    APP_SECRET,
    FakeGraph,
    comment_payload,
    make_page,
    post_webhook,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _signed():
    with override_settings(
        WHATSAPP_APP_SECRET=APP_SECRET,
        INSTAGRAM_APP_SECRET="",
        FACEBOOK_VERIFY_TOKEN="fb-verify",
    ):
        yield


@pytest.fixture(autouse=True)
def graph():
    fake = FakeGraph()
    with patch("apps.facebook.providers.meta.requests.request", side_effect=fake):
        yield fake


def test_a_page_comment_is_classified_and_reclassified_on_edit(client):
    _account, _user, conn = make_page()
    payload = comment_payload(conn.page_id, "u1", "Is this in stock?")
    post_webhook(client, payload)

    comment = FacebookComment.objects.get()
    run = AudienceSignalRun.objects.get(fb_comment=comment)
    assert run.channel == "facebook"
    assert {(s.category, s.subcategory) for s in run.signals.all()} == {
        ("product_enquiry", "stock")
    }

    value = payload["entry"][0]["changes"][0]["value"]
    post_webhook(
        client,
        comment_payload(
            conn.page_id,
            "u1",
            "Never arrived, refund please",
            comment_id=value["comment_id"],
            post_id=value["post_id"],
            verb="edited",
        ),
    )
    comment.refresh_from_db()
    assert AudienceSignalRun.objects.filter(fb_comment=comment).count() == 2
    assert {(s.category, s.subcategory) for s in services.current_signals(comment)} == {
        ("complaint", "delivery_delay"),
        ("complaint", "dissatisfaction"),
    }


def test_a_redelivered_webhook_adds_no_second_run(client):
    _account, _user, conn = make_page()
    payload = comment_payload(conn.page_id, "u1", "how much?")
    post_webhook(client, payload)
    post_webhook(client, payload)
    assert AudienceSignalRun.objects.count() == 1
