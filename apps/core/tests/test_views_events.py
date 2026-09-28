"""apps.core.views_events: the subscribe half of real-time updates.

Test settings have REDIS_URL="" (automator/settings_test.py), so these exercise the
heartbeat-only fallback path — the one path guaranteed to run the same with or without a real
Redis behind it, and the one that matters most: an unreachable Redis must degrade to "no
real-time nudges, polling still works", never to a broken or hanging connection.
"""

import asyncio

import pytest
from django.contrib.auth.models import User
from django.test import AsyncClient

from apps.accounts.models import Account, Membership


async def _first_chunk(response, timeout=2):
    """One chunk from the streaming body, or None if nothing arrived in time."""
    aiter = response.streaming_content.__aiter__()
    try:
        return await asyncio.wait_for(aiter.__anext__(), timeout=timeout)
    except (TimeoutError, StopAsyncIteration):
        return None


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_anonymous_is_rejected():
    client = AsyncClient()
    response = await client.get("/events/stream/")
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_a_member_gets_a_streaming_response_with_a_retry_hint():
    from asgiref.sync import sync_to_async

    def make_user():
        user = User.objects.create_user("events-user", "events@example.com", "pw")
        account = Account.objects.create(company_name="Events Co")
        Membership.objects.create(
            user=user, account=account, role=Membership.Role.OWNER
        )
        return user

    user = await sync_to_async(make_user)()

    client = AsyncClient()
    await sync_to_async(client.force_login)(user)
    response = await client.get("/events/stream/")

    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"
    assert response["Cache-Control"] == "no-cache"

    first = await _first_chunk(response)
    assert first is not None
    assert first.decode().startswith("retry:")


@pytest.mark.asyncio
async def test_the_no_redis_branch_sends_heartbeats_not_silence(monkeypatch):
    """No REDIS_URL (as in tests) must degrade to heartbeat-only, not hang or error — the
    client's own poll fallback keeps everything working either way. Patches asyncio.sleep so
    this doesn't need to wait out a real HEARTBEAT_SECONDS to prove it."""
    from apps.core import views_events

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 2:
            raise asyncio.CancelledError  # stop the otherwise-infinite loop

    monkeypatch.setattr(views_events.asyncio, "sleep", fake_sleep)

    chunks = []
    try:
        async for chunk in views_events._stream(account_id=1):
            chunks.append(chunk)
            if len(chunks) >= 3:
                break
    except asyncio.CancelledError:
        pass

    assert chunks[0].startswith("retry:")
    assert all(c.startswith(":") for c in chunks[1:])
    assert slept and slept[0] == views_events.HEARTBEAT_SECONDS
