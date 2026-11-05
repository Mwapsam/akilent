"""Sessions: read from Redis, kept in PostgreSQL (``SESSION_ENGINE``).

Django's ``cached_db`` store, with every cache call guarded. Stock ``cached_db`` already falls
back to the database when a cache *read* fails and logs a failed write on save, but three calls
can still raise when Redis is down: the cache write after a database fallback in ``load``, the
cache lookup in ``exists`` (used when a new key is made, at sign-in) and the cache delete in
``delete`` (sign-out). Here each of those is logged and skipped, so with Redis down, flushed or
restarted, people stay signed in through PostgreSQL, the source of truth.

Ending sessions can't rely on Redis either. A cached copy is read without asking PostgreSQL, so
a sign-out that couldn't delete it would leave it usable once Redis came back with its data.
Each person has a session *generation* in PostgreSQL (``accounts.SessionSecurity``), loaded
with the user in the query every signed-in request already makes; each session records the
generation it was signed in under, and one that doesn't match is signed out
(``SessionRefreshMiddleware``). It goes up whenever a cached copy might have outlived its end
(a sign-out or key rotation Redis didn't confirm) and on security events (``end_all_sessions``).
A password change needs nothing extra: Django checks every session against the password hash.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends import cached_db
from django.contrib.sessions.backends.db import SessionStore as DBStore
from django.core.exceptions import ObjectDoesNotExist

if TYPE_CHECKING:
    from django.core.cache.backends.base import BaseCache

logger = logging.getLogger("django.contrib.sessions")

GENERATION = (
    "_session_generation"  # the person's generation when this session signed in
)
SIGNED_IN_AT = "_signed_in_at"  # for the absolute lifetime (SESSION_ABSOLUTE_AGE)


def generation_of(user) -> int:
    """The person's current session generation (0 without a row). No query for a user from
    accounts.backends.EmailBackend, which joins it."""
    try:
        return user.session_security.generation
    except (ObjectDoesNotExist, AttributeError):
        return 0


def end_all_sessions(user_id) -> int:
    """End every session ``user_id`` has, in Redis or not, durably: raise their generation.
    Returns the new generation."""
    from django.db.models import F

    from apps.accounts.models import SessionSecurity

    row, created = SessionSecurity.objects.get_or_create(
        user_id=user_id, defaults={"generation": 1}
    )
    if not created:
        SessionSecurity.objects.filter(pk=row.pk).update(generation=F("generation") + 1)
        row.refresh_from_db(fields=["generation"])
    return row.generation


class SessionStore(cached_db.SessionStore):
    _cache: BaseCache  # set by cached_db's __init__; not in Django's stubs
    _cache_delete_failed = False

    def load(self):
        try:
            data = self._cache.get(self.cache_key)
        except Exception:
            logger.warning("Session cache unavailable on read; using the database")
            data = None
        if data is not None:
            return data
        s = self._get_session_from_db()  # type: ignore[attr-defined]  # not in the stubs
        if not s:
            return {}
        data = self.decode(s.session_data)
        try:
            self._cache.set(
                self.cache_key, data, self.get_expiry_age(expiry=s.expire_date)
            )
        except Exception:
            logger.warning("Session cache unavailable on write; continuing")
        return data

    def exists(self, session_key):
        if not session_key:
            return False
        try:
            if (self.cache_key_prefix + session_key) in self._cache:
                return True
        except Exception:
            logger.warning("Session cache unavailable on lookup; using the database")
        return DBStore.exists(self, session_key)

    def delete(self, session_key=None):
        DBStore.delete(self, session_key)
        if session_key is None:
            if self.session_key is None:
                return
            session_key = self.session_key
        try:
            self._cache.delete(self.cache_key_prefix + session_key)
        except Exception:
            self._cache_delete_failed = True
            logger.warning(
                "Session cache unavailable on delete; the database row is gone"
            )

    def flush(self):
        """Sign-out. If the cached copy couldn't be deleted, end the person's sessions durably,
        so that copy is refused if Redis comes back with it."""
        user_id = self.get(SESSION_KEY)
        self._cache_delete_failed = False
        super().flush()
        if user_id and self._cache_delete_failed:
            end_all_sessions(user_id)
            logger.warning(
                "Sign-out without Redis: ended every session of user %s", user_id
            )

    def cycle_key(self):
        """A new key for the same session (sign-in). If the old key's cached copy couldn't be
        deleted, end the person's other sessions and carry this one on the new generation."""
        user_id = self.get(SESSION_KEY)
        self._cache_delete_failed = False
        super().cycle_key()
        if user_id and self._cache_delete_failed:
            self[GENERATION] = end_all_sessions(user_id)
            logger.warning(
                "Key change without Redis: ended other sessions of user %s", user_id
            )


def stamp_signed_in(sender, request, user, **kwargs):
    """``user_logged_in``: the session starts on the person's current generation, and its
    absolute lifetime starts now. Read from the database, not the user object, which may hold
    an older answer (one query, at sign-in only)."""
    from apps.accounts.models import SessionSecurity

    current = (
        SessionSecurity.objects.filter(user_id=user.pk)
        .values_list("generation", flat=True)
        .first()
    )
    request.session[GENERATION] = current or 0
    request.session[SIGNED_IN_AT] = int(time.time())


# How often a signed-in person's session is extended while they're active. Each extension is
# one write (PostgreSQL, then Redis); between them a request writes nothing. The inactivity
# limit is SESSION_COOKIE_AGE after the last extension, so between it and it less this interval
# after the last activity.
REFRESH_EVERY = 5 * 60
REFRESHED_AT = "_refreshed_at"


def _is_activity(request) -> bool:
    """A person doing something: a page, a page change, a drawer or modal opening, or any
    change (POST and the like). Not what an idle open tab sends by itself: htmx polling (a
    background GET) and the live-updates stream."""
    from apps.core.htmx import is_background, is_overlay

    if "text/event-stream" in request.headers.get("Accept", ""):
        return False
    if request.method in ("GET", "HEAD") and is_background(request):
        return is_overlay(request)
    return True


def _still_valid(request, session, now: int) -> bool:
    """Within the absolute lifetime, and signed in under the person's current generation."""
    signed_in_at = session.get(SIGNED_IN_AT)
    if signed_in_at is None:
        session[SIGNED_IN_AT] = now  # signed in before this existed: counts from now
    elif now - int(signed_in_at) > settings.SESSION_ABSOLUTE_AGE:
        return False
    user = request.user
    if not user.is_authenticated:
        return True  # Django's own checks already refused it
    return int(session.get(GENERATION, 0)) == generation_of(user)


class SessionRefreshMiddleware:
    """For a signed-in session, on every request:

    - sign it out if it's past ``SESSION_ABSOLUTE_AGE`` or its generation is stale (no query:
      the generation comes with the user, the rest is in the session);
    - else extend it at most once every ``REFRESH_EVERY`` seconds of activity, so the 8-hour
      limit counts from the last activity, without a write on every request
      (``SESSION_SAVE_EVERY_REQUEST`` stays off). Setting the timestamp marks the session
      modified; SessionMiddleware then saves it with a new expiry."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = getattr(request, "session", None)
        if session is not None and session.get(SESSION_KEY):
            now = int(time.time())
            if not _still_valid(request, session, now):
                from django.contrib.auth import logout

                logout(request)  # ends the session; request.user becomes anonymous
            elif (
                _is_activity(request)
                and now - int(session.get(REFRESHED_AT, 0)) >= REFRESH_EVERY
            ):
                session[REFRESHED_AT] = now
        return self.get_response(request)
