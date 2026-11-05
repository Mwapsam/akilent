"""Sessions read from Redis and kept in PostgreSQL, extended while active (apps/core/sessions.py).

The 8 hours count from the last activity, not from sign-in, with at most one write every five
minutes; with the cache down or cleared people stay signed in through the database; signing
out ends the session everywhere; sessions made before the switch carry over.
"""

import datetime
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.cache import caches
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.core import sessions

pytestmark = pytest.mark.django_db

PAGE = "/auth/login/"  # any page: what matters is who the request is signed in as
POLL = {"HTTP_HX_REQUEST": "true"}
OVERLAY = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "overlay-body"}


@pytest.fixture(autouse=True)
def _engine(settings):
    settings.SESSION_ENGINE = "apps.core.sessions"
    caches[settings.SESSION_CACHE_ALIAS].clear()


class Clock:
    """Moves Django's clock (session expiry) and the wall clock (the refresh interval)."""

    def __init__(self):
        self.now = timezone.now()

    def forward(self, **kw):
        self.now += datetime.timedelta(**kw)

    @contextmanager
    def running(self):
        with (
            patch("django.utils.timezone.now", lambda: self.now),
            patch("time.time", lambda: self.now.timestamp()),
        ):
            yield


@pytest.fixture
def clock():
    c = Clock()
    with c.running():
        yield c


@pytest.fixture
def person():
    user = User.objects.create_user("s", "s@example.com", "pass")
    client = Client()
    client.force_login(user)
    return user, client


def _signed_in(client, **extra) -> bool:
    return client.get(PAGE, **extra).wsgi_request.user.is_authenticated


def _writes(q) -> int:
    return sum(
        x["sql"].lstrip().upper().startswith(("UPDATE", "INSERT"))
        and '"django_session"' in x["sql"]
        for x in q.captured_queries
    )


def _key(client):
    return client.cookies[settings.SESSION_COOKIE_NAME].value


def test_activity_within_five_minutes_writes_nothing(person, clock):
    _, client = person
    client.get(PAGE)  # the first activity extends it once

    with CaptureQueriesContext(connection) as q:
        for _ in range(5):
            clock.forward(seconds=50)
            client.get(PAGE)

    assert _writes(q) == 0


def test_activity_after_five_minutes_extends_the_session(person, clock):
    _, client = person
    client.get(PAGE)
    before = Session.objects.get(pk=_key(client)).expire_date

    clock.forward(minutes=5, seconds=1)
    with CaptureQueriesContext(connection) as q:
        client.get(PAGE)

    assert _writes(q) == 1
    after = Session.objects.get(pk=_key(client)).expire_date
    assert after - before == datetime.timedelta(minutes=5, seconds=1)


def test_an_idle_tab_polling_doesnt_keep_the_session_alive(person, clock):
    _, client = person
    client.get(PAGE)
    clock.forward(minutes=6)

    with CaptureQueriesContext(connection) as q:
        client.get(PAGE, **POLL)  # htmx polling: a background GET
        client.get(PAGE, HTTP_ACCEPT="text/event-stream")  # the live-updates stream
    assert _writes(q) == 0

    with CaptureQueriesContext(connection) as q:
        client.get(PAGE, **OVERLAY)  # opening a drawer is someone doing something
    assert _writes(q) == 1


def test_active_people_stay_signed_in_past_eight_hours(person, clock):
    _, client = person
    for _ in range(5):  # ten hours, with something done every two
        clock.forward(hours=2)
        assert _signed_in(client)


def test_eight_hours_without_activity_signs_out(person, clock):
    _, client = person
    client.get(PAGE)
    clock.forward(hours=7, minutes=50)
    clock.forward(
        minutes=11
    )  # 8h01 since the last activity; polling meanwhile counts for nothing

    assert not _signed_in(client, **POLL)
    assert not _signed_in(client)


def test_a_cleared_cache_keeps_people_signed_in(person):
    _, client = person
    assert _signed_in(client)

    caches[settings.SESSION_CACHE_ALIAS].clear()

    assert _signed_in(client)
    assert caches[settings.SESSION_CACHE_ALIAS].get(
        sessions.SessionStore.cache_key_prefix + _key(client)
    )


class _Down:
    """A cache whose every call fails, like Redis stopped or being recreated."""

    def __getattr__(self, name):
        def fail(*a, **kw):
            raise ConnectionError("Redis is down")

        return fail

    def __contains__(self, key):
        raise ConnectionError("Redis is down")


def test_with_redis_down_people_stay_signed_in_and_can_sign_out(person, clock):
    user, client = person
    key = _key(client)
    # The store picks its cache up when it's made, so this is what every request sees.
    with patch(
        "django.contrib.sessions.backends.cached_db.caches",
        {settings.SESSION_CACHE_ALIAS: _Down()},
    ):
        assert _signed_in(client)
        clock.forward(minutes=6)
        assert _signed_in(client)  # the extension is saved to the database
        fresh = Client()
        fresh.force_login(user)  # a new sign-in makes a key without the cache
        assert _signed_in(fresh)
        resp = client.post("/auth/logout/")
        assert resp.status_code in (200, 302)

    assert not Session.objects.filter(pk=key).exists()


def test_signing_out_ends_the_session_everywhere(person):
    _, client = person
    key = _key(client)
    client.get(PAGE)

    client.post("/auth/logout/")

    assert not Session.objects.filter(pk=key).exists()
    assert (
        caches[settings.SESSION_CACHE_ALIAS].get(
            sessions.SessionStore.cache_key_prefix + key
        )
        is None
    )
    old = Client()
    old.cookies[settings.SESSION_COOKIE_NAME] = key
    assert not _signed_in(old)


def test_sessions_from_before_the_switch_carry_over(settings):
    from django.contrib.sessions.backends.db import SessionStore as DBStore

    user = User.objects.create_user("old", "old@example.com", "pass")
    settings.SESSION_ENGINE = "django.contrib.sessions.backends.db"
    client = Client()
    client.force_login(user)  # signed in before the deploy
    assert DBStore(_key(client)).load()  # a plain database session, nothing in Redis

    settings.SESSION_ENGINE = "apps.core.sessions"  # after the deploy
    assert _signed_in(client)


def test_the_session_settings_hold():
    """8 hours of inactivity, no write on every request, sessions on their own cache alias
    (the same Redis today; SESSION_REDIS_URL moves them to a dedicated one)."""
    assert settings.SESSION_COOKIE_AGE == 8 * 60 * 60
    assert settings.SESSION_SAVE_EVERY_REQUEST is False
    assert settings.SESSION_CACHE_ALIAS == "sessions"
    assert sessions.REFRESH_EVERY == 5 * 60
    assert "apps.core.sessions.SessionRefreshMiddleware" in settings.MIDDLEWARE


# --- Ending sessions durably: Redis can come back with an old copy -------------------------


@contextmanager
def _redis_down():
    with patch(
        "django.contrib.sessions.backends.cached_db.caches",
        {settings.SESSION_CACHE_ALIAS: _Down()},
    ):
        yield


def _cached(key):
    return caches[settings.SESSION_CACHE_ALIAS].get(
        sessions.SessionStore.cache_key_prefix + key
    )


def _redis_returns_with(key, data):
    """Redis comes back with the copy it held before the outage."""
    caches[settings.SESSION_CACHE_ALIAS].set(
        sessions.SessionStore.cache_key_prefix + key, data, 8 * 60 * 60
    )


def _thief(key) -> Client:
    """Someone else, on another worker, presenting a copied cookie."""
    c = Client()
    c.cookies[settings.SESSION_COOKIE_NAME] = key
    return c


def test_a_cookie_copied_before_a_sign_out_during_an_outage_stays_dead(person):
    user, client = person
    key = _key(client)
    client.get(PAGE)
    held = _cached(key)  # what Redis keeps through the outage
    assert held

    with _redis_down():
        client.post("/auth/logout/")
    _redis_returns_with(key, held)

    assert not Session.objects.filter(pk=key).exists()
    assert not _signed_in(_thief(key))  # refused although Redis served the old copy
    assert sessions.generation_of(type(user).objects.get(pk=user.pk)) == 1


def test_an_ordinary_sign_out_ends_only_that_session(person):
    user, client = person
    phone = Client()
    phone.force_login(user)

    client.post("/auth/logout/")  # Redis up: the cached copy is deleted

    assert _signed_in(phone)  # the person's other device stays signed in
    assert sessions.generation_of(type(user).objects.get(pk=user.pk)) == 0


def test_an_mfa_reset_during_an_outage_ends_every_session(person):
    from apps.accounts.management.commands.reset_user_mfa import _end_sessions

    user, client = person
    phone = Client()
    phone.force_login(user)
    keys = [_key(client), _key(phone)]
    for c in (client, phone):
        c.get(PAGE)
    held = {k: _cached(k) for k in keys}

    with _redis_down():
        assert _end_sessions(user.pk) == 2
    for k in keys:
        _redis_returns_with(k, held[k])

    assert not any(_signed_in(_thief(k)) for k in keys)


def test_a_password_change_during_an_outage_ends_old_sessions(person):
    user, client = person
    key = _key(client)
    client.get(PAGE)
    held = _cached(key)

    with _redis_down():
        user.set_password("a new one")
        user.save()
    _redis_returns_with(key, held)

    assert not _signed_in(_thief(key))  # Django checks the password hash


def test_a_new_sign_in_after_the_outage_works(person):
    user, client = person
    with _redis_down():
        client.post("/auth/logout/")
    again = Client()
    again.force_login(user)  # starts on the raised generation

    assert _signed_in(again)


def test_sessions_end_seven_days_after_sign_in_however_active(person, clock):
    _, client = person
    for _ in range(7 * 4):  # every six hours for a week
        clock.forward(hours=6)
        assert _signed_in(client)
    clock.forward(hours=6)

    assert not _signed_in(client)


def test_a_session_from_before_the_deploy_starts_its_week_then(person, clock):
    _, client = person
    s = Session.objects.get(pk=_key(client))
    data = s.get_decoded()
    for k in (sessions.GENERATION, sessions.SIGNED_IN_AT):
        data.pop(k, None)  # as written before this change
    s.session_data = sessions.SessionStore().encode(data)
    s.save()
    caches[settings.SESSION_CACHE_ALIAS].clear()

    assert _signed_in(
        client
    )  # generation 0 matches: no one is signed out by the deploy
    for _ in range(
        7 * 4
    ):  # active every six hours: the week counts from that first request
        clock.forward(hours=6)
        assert _signed_in(client)
    clock.forward(hours=6)
    assert not _signed_in(client)


def test_checking_the_generation_adds_no_query(person):
    _, client = person
    client.get(PAGE)

    with CaptureQueriesContext(connection) as q:
        assert _signed_in(client)

    assert not any(
        'FROM "accounts_sessionsecurity"' in x["sql"] for x in q.captured_queries
    )  # joined into the user's query
    assert any("accounts_sessionsecurity" in x["sql"] for x in q.captured_queries)
