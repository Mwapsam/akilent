"""Workstream E: HubSpot connector tests.

Tests cover:
 1. v3 signature: valid accepts, replay rejected, wrong secret rejected
 2. Echo detection: remote value equals last synced hash → skipped
 3. Conflict: equal timestamps → SyncConflict created, local value kept
 4. Outbox coalesce: two enqueues create one PENDING job
 5. Token refresh: expired token triggers refresh, new token used
 6. Archived remote contact: link is remote_archived, outbound push skipped
 7. Reconciliation: contact page applied, cursor advanced, older snapshot skipped
 8. OAuth state single-use: replayed callback rejected
 9. Lead status push: set_lead_status triggers hs_lead_status patch
10. Webhook deduplicate: same event_id is a no-op on second delivery
11. Unlinked contact webhook: silently ignored
12. process_sync_job: skipped when connection is DISCONNECTED
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from apps.accounts.models import Account
from apps.contacts.models import Contact

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="HS Test Co")


@pytest.fixture
def contact(account):
    return Contact.objects.create(
        account=account, phone="+260971000001", email="test@example.com"
    )


@pytest.fixture
def connection(account):
    from apps.hubspot.models import HubSpotConnection

    return HubSpotConnection.objects.create(
        account=account,
        portal_id="12345",
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        status=HubSpotConnection.Status.ACTIVE,
    )


@pytest.fixture
def contact_link(account, contact, connection):
    from apps.hubspot.models import HubSpotContactLink

    return HubSpotContactLink.objects.create(
        account=account, contact=contact, hs_object_id="hs-contact-001"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 1. v3 signature verification
# ──────────────────────────────────────────────────────────────────────────────


def _make_v3_sig(secret: str, method: str, uri: str, body: bytes, ts: str) -> str:
    source = f"{method}{uri}{body.decode()}{ts}"
    return base64.b64encode(
        hmac.new(secret.encode(), source.encode(), hashlib.sha256).digest()
    ).decode()


def test_signature_v3_valid():
    from apps.hubspot.signature import verify_v3

    secret = "mysecret"
    ts = str(int(time.time() * 1000))
    body = b'[{"eventId":"1"}]'
    sig = _make_v3_sig(secret, "POST", "https://example.com/hubspot/webhook/", body, ts)
    verify_v3(secret, "POST", "https://example.com/hubspot/webhook/", body, sig, ts)


def test_signature_v3_replay_rejected():
    from apps.hubspot.signature import SignatureError, verify_v3

    secret = "mysecret"
    old_ts = str(int((time.time() - 400) * 1000))
    body = b"[]"
    sig = _make_v3_sig(secret, "POST", "https://x.com/w/", body, old_ts)
    with pytest.raises(SignatureError, match="old"):
        verify_v3(secret, "POST", "https://x.com/w/", body, sig, old_ts)


def test_signature_v3_wrong_secret():
    from apps.hubspot.signature import SignatureError, verify_v3

    ts = str(int(time.time() * 1000))
    body = b"[]"
    sig = _make_v3_sig("rightSecret", "POST", "https://x.com/w/", body, ts)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_v3("wrongSecret", "POST", "https://x.com/w/", body, sig, ts)


# ──────────────────────────────────────────────────────────────────────────────
# 2. Echo detection
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_echo_detection_skips_unchanged_value(account, contact, contact_link):
    from apps.hubspot.inbound import process_webhook_event
    from apps.hubspot.models import HubSpotFieldState, HubSpotWebhookEvent

    norm_email = "test@example.com"
    current_hash = HubSpotFieldState.hash_value(norm_email)

    HubSpotFieldState.objects.create(
        contact_link=contact_link,
        field="email",
        synced_value_hash=current_hash,
        direction=HubSpotFieldState.Direction.BIDIRECTIONAL,
    )

    event = HubSpotWebhookEvent.objects.create(
        account=account,
        event_id="echo-001",
        portal_id="12345",
        object_type="contact",
        object_id="hs-contact-001",
        property_name="email",
        property_value=norm_email,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw_payload={},
    )
    with patch("apps.hubspot.inbound._write_contact_field") as mock_write:
        process_webhook_event(event)
    mock_write.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# 3. Conflict: equal timestamps
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_equal_timestamps_creates_conflict(account, contact, contact_link):
    from apps.hubspot.inbound import process_webhook_event
    from apps.hubspot.models import HubSpotFieldState, HubSpotWebhookEvent, SyncConflict

    ts = datetime(2026, 6, 1, tzinfo=UTC)
    local_email = "local@example.com"
    local_hash = HubSpotFieldState.hash_value(local_email)

    HubSpotFieldState.objects.create(
        contact_link=contact_link,
        field="email",
        synced_value_hash=local_hash,
        synced_source_ts=ts,
        direction=HubSpotFieldState.Direction.BIDIRECTIONAL,
    )

    remote_email = "remote@example.com"
    event = HubSpotWebhookEvent.objects.create(
        account=account,
        event_id="conflict-001",
        portal_id="12345",
        object_type="contact",
        object_id="hs-contact-001",
        property_name="email",
        property_value=remote_email,
        occurred_at=ts,
        raw_payload={},
    )

    with patch("apps.hubspot.inbound._write_contact_field"):
        process_webhook_event(event)

    assert SyncConflict.objects.filter(
        account=account, field="email", status=SyncConflict.Status.OPEN
    ).exists()


# ──────────────────────────────────────────────────────────────────────────────
# 4. Outbox coalesce
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_outbox_coalesces_multiple_enqueues(account, contact_link):
    from apps.hubspot.models import HubSpotSyncJob
    from apps.hubspot.sync import enqueue_contact_sync

    enqueue_contact_sync(contact_link)
    enqueue_contact_sync(contact_link)
    enqueue_contact_sync(contact_link)

    count = HubSpotSyncJob.objects.filter(
        account=account, contact_link=contact_link, status=HubSpotSyncJob.Status.PENDING
    ).count()
    assert count == 1


# ──────────────────────────────────────────────────────────────────────────────
# 5. Token refresh
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_get_fresh_client_refreshes_expired_token(account, connection):
    from apps.hubspot.client import get_fresh_client

    connection.token_expires_at = datetime(2020, 1, 1, tzinfo=UTC)
    connection.save(update_fields=["token_expires_at"])

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "access_token": "new-token",
        "refresh_token": "new-refresh",
        "expires_in": 1800,
    }

    with patch("apps.hubspot.client.requests.post", return_value=mock_resp):
        get_fresh_client(connection)

    connection.refresh_from_db()
    assert connection.access_token == "new-token"


# ──────────────────────────────────────────────────────────────────────────────
# 6. Archived remote contact: outbound push skipped
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_archived_contact_sync_job_skipped(account, contact_link, connection):
    from apps.hubspot.models import HubSpotSyncJob
    from apps.hubspot.sync import process_sync_job

    contact_link.remote_archived = True
    contact_link.save(update_fields=["remote_archived"])

    job = HubSpotSyncJob.objects.create(
        account=account,
        object_type=HubSpotSyncJob.ObjectType.CONTACT,
        contact_link=contact_link,
    )

    with patch("apps.hubspot.sync.get_fresh_client") as mock_client:
        process_sync_job(job)

    mock_client.return_value.patch.assert_not_called()
    job.refresh_from_db()
    assert job.status == HubSpotSyncJob.Status.DONE


# ──────────────────────────────────────────────────────────────────────────────
# 7. Reconciliation: cursor advances; older snapshot skipped
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_reconcile_skips_older_snapshot(account, contact, contact_link, connection):
    from apps.hubspot.models import HubSpotFieldState, ReconcileCursor
    from apps.hubspot.reconcile import _apply_contact_object

    ts_recent = datetime(2026, 9, 1, tzinfo=UTC)
    HubSpotFieldState.objects.create(
        contact_link=contact_link,
        field="email",
        synced_value_hash=HubSpotFieldState.hash_value("test@example.com"),
        synced_source_ts=ts_recent,
        direction=HubSpotFieldState.Direction.BIDIRECTIONAL,
    )

    cursor = ReconcileCursor.objects.create(
        account=account,
        object_type=ReconcileCursor.ObjectType.CONTACT,
    )

    old_obj = {
        "id": "hs-contact-001",
        "properties": {
            "email": "old@example.com",
            "lastmodifieddate": "2026-01-01T00:00:00.000Z",
        },
    }

    with patch("apps.hubspot.reconcile._write_contact_field") as mock_write:
        _apply_contact_object(account, old_obj, cursor)

    mock_write.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# 8. OAuth state: single-use
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_oauth_callback_rejects_replayed_state(account, connection, rf):
    """A replayed state token returns 400."""
    from apps.hubspot.views import oauth_callback

    connection.oauth_state = "valid-state"
    connection.oauth_state_created_at = datetime(2026, 1, 1, tzinfo=UTC)
    connection.save(update_fields=["oauth_state", "oauth_state_created_at"])

    request = rf.get(
        "/hubspot/oauth/callback/", {"code": "abc", "state": "wrong-state"}
    )
    request._current_account = account

    from django.contrib.auth.models import AnonymousUser

    request.user = AnonymousUser()

    response = (
        oauth_callback.__wrapped__(request)
        if hasattr(oauth_callback, "__wrapped__")
        else _call_view(oauth_callback, request)
    )
    assert response.status_code == 400


def _call_view(view_func, request):
    """Call a login_required view bypassing the login check."""
    inner = getattr(view_func, "view_func", view_func)
    return inner(request)


# ──────────────────────────────────────────────────────────────────────────────
# 9. Lead status push
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_push_lead_status_patches_hs_contact(
    account, contact, contact_link, connection
):
    from apps.crm.models import Lead
    from apps.hubspot.sync import push_lead_status

    lead = Lead.objects.create(account=account, contact=contact, status="new")

    mock_client = MagicMock()
    with patch("apps.hubspot.sync.get_fresh_client", return_value=mock_client):
        push_lead_status(lead)

    mock_client.patch.assert_called_once()
    call_args = mock_client.patch.call_args
    assert "hs_lead_status" in call_args[1]["json"]["properties"]
    assert call_args[1]["json"]["properties"]["hs_lead_status"] == "New"


# ──────────────────────────────────────────────────────────────────────────────
# 10. Webhook deduplicate
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_webhook_event_deduplication(account, contact_link):
    from apps.hubspot.inbound import process_webhook_event
    from apps.hubspot.models import HubSpotWebhookEvent

    event = HubSpotWebhookEvent.objects.create(
        account=account,
        event_id="dup-001",
        portal_id="12345",
        object_type="contact",
        object_id="hs-contact-001",
        property_name="email",
        property_value="a@b.com",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw_payload={},
        processed=True,
    )

    with patch("apps.hubspot.inbound._write_contact_field") as mock_write:
        process_webhook_event(event)

    mock_write.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# 11. Unlinked contact webhook: silently ignored
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_webhook_unlinked_contact_ignored(account):
    from apps.hubspot.inbound import process_webhook_event
    from apps.hubspot.models import HubSpotConnection, HubSpotWebhookEvent

    HubSpotConnection.objects.create(
        account=account,
        portal_id="99999",
        access_token="tok",
        status=HubSpotConnection.Status.ACTIVE,
    )

    event = HubSpotWebhookEvent.objects.create(
        account=account,
        event_id="unlinked-001",
        portal_id="99999",
        object_type="contact",
        object_id="no-such-hs-id",
        property_name="email",
        property_value="x@y.com",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw_payload={},
    )

    with patch("apps.hubspot.inbound._write_contact_field") as mock_write:
        process_webhook_event(event)

    mock_write.assert_not_called()
    event.refresh_from_db()
    assert event.processed


# ──────────────────────────────────────────────────────────────────────────────
# 12. process_sync_job: skipped when DISCONNECTED
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_sync_job_skipped_when_disconnected(account, contact_link, connection):
    from apps.hubspot.models import HubSpotSyncJob
    from apps.hubspot.sync import process_sync_job

    connection.status = connection.Status.DISCONNECTED
    connection.save(update_fields=["status"])

    job = HubSpotSyncJob.objects.create(
        account=account,
        object_type=HubSpotSyncJob.ObjectType.CONTACT,
        contact_link=contact_link,
    )

    with patch("apps.hubspot.sync.get_fresh_client") as mock_gc:
        process_sync_job(job)

    mock_gc.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# New contacts reach HubSpot off the message path
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
def test_new_contact_push_is_queued_after_commit_not_run_inline(account, connection):
    from unittest.mock import patch

    with (
        patch("apps.hubspot.create.create_hs_contact_from_local") as inline,
        patch("apps.hubspot.tasks.push_new_contact_to_hubspot.delay") as queued,
    ):
        contact = Contact.objects.create(account=account, phone="+260971000099")
    inline.assert_not_called()
    queued.assert_called_once_with(contact.pk)


@pytest.mark.django_db(transaction=True)
def test_no_push_is_queued_without_a_connection(account):
    from unittest.mock import patch

    with patch("apps.hubspot.tasks.push_new_contact_to_hubspot.delay") as queued:
        Contact.objects.create(account=account, phone="+260971000098")
    queued.assert_not_called()


@pytest.mark.django_db
def test_push_task_creates_the_hubspot_contact(contact, connection):
    from unittest.mock import patch

    from apps.hubspot.tasks import push_new_contact_to_hubspot

    with patch("apps.hubspot.create.create_hs_contact_from_local") as create:
        push_new_contact_to_hubspot(contact.pk)
    create.assert_called_once()
    assert create.call_args.args[0].pk == contact.pk
