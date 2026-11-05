"""Sessions against a real Redis: restart, flush, stop, hang, recreate, and a copied cookie after
a sign-out Redis didn't see (apps/core/sessions.py, docs/design/ui-quality-log.md Entry 24-25).

Run on the host that runs Docker, with the app's settings and environment (as the web container
has them), for a test user:

    QA_SESSION_USER=someone@example.com REDIS_CONTAINER=akilent-redis-1 \\
        python tools/qa/session_redis_check.py

It signs that user in and out, stops, pauses and recreates REDIS_CONTAINER (recreating it with
REDIS_IMAGE and REDIS_RUN_ARGS, so only use it where that's what you want), and flushes the
sessions cache's Redis database. Each step prints PASS or FAIL. Separate worker processes are
real processes: the copied-cookie check runs its sign-out and its reuse in two of them.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

CONTAINER = os.environ.get("REDIS_CONTAINER", "akilent-uiqa-redis")
IMAGE = os.environ.get("REDIS_IMAGE", "redis:7.4-alpine")
RUN_ARGS = os.environ.get("REDIS_RUN_ARGS", "-p 127.0.0.1:6379:6379")


def setup():
    sys.path.insert(0, str(Path.cwd()))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "automator.settings")
    import django

    django.setup()
    from django.conf import settings

    settings.ALLOWED_HOSTS = ["*"]


def sh(cmd: str) -> str:
    """A docker command; failures are read from the output, not raised."""
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=False)  # noqa: S603 - our own docker commands
    return (r.stdout + r.stderr).strip()


def tools():
    from django.conf import settings
    from django.contrib.auth.models import User
    from django.core.cache import caches
    from django.test import Client

    from apps.core.sessions import SessionStore

    def user():
        return User.objects.get(email__iexact=os.environ["QA_SESSION_USER"])

    def client(key=None, login=False):
        c = Client()
        if login:
            c.force_login(user())
        elif key:
            c.cookies[settings.SESSION_COOKIE_NAME] = key
        return c

    def key_of(c):
        return c.cookies[settings.SESSION_COOKIE_NAME].value

    def signed_in(c):
        return c.get("/auth/login/").wsgi_request.user.is_authenticated

    def cached(key):
        try:
            cache = caches[settings.SESSION_CACHE_ALIAS]
            return cache.get(SessionStore.cache_key_prefix + key) is not None
        except Exception as e:
            return f"unreachable ({type(e).__name__})"

    return client, key_of, signed_in, cached


def phase(name: str, key: str = "") -> None:
    """One step in its own process, like a separate worker."""
    setup()
    from django.contrib.sessions.models import Session

    client, key_of, signed_in, cached = tools()
    if name == "login":
        c = client(login=True)
        c.get("/auth/login/")
        print(key_of(c))
    elif name == "logout":
        r = client(key).post("/auth/logout/")
        print(r.status_code, Session.objects.filter(pk=key).exists())
    elif name == "probe":
        print(cached(key), signed_in(client(key)))


def sub(*args: str) -> list[str]:
    out = subprocess.run(  # noqa: S603 - this script, as a separate worker process
        [sys.executable, __file__, "--phase", *args],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return out.strip().splitlines()[-1].split()


def main() -> int:
    setup()
    from django.conf import settings
    from django.contrib.sessions.models import Session

    client, key_of, signed_in, cached = tools()
    results = []

    def step(name, ok, **info):
        results.append(bool(ok))
        print("PASS" if ok else "FAIL", name, info or "")

    alias = cast("dict[str, Any]", settings.CACHES[settings.SESSION_CACHE_ALIAS])
    db = str(alias["LOCATION"]).rsplit("/", 1)[
        -1
    ]  # the sessions cache's Redis database
    c = client(login=True)
    step("signed in, session in Redis", signed_in(c) and cached(key_of(c)) is True)
    sh(f"docker restart {CONTAINER}")
    step("after Redis restart", signed_in(c))
    sh(f"docker exec {CONTAINER} redis-cli -n {db} FLUSHDB")
    step(
        "after flush: from PostgreSQL, cache refilled",
        signed_in(c) and cached(key_of(c)) is True,
    )
    sh(f"docker pause {CONTAINER}")
    t0 = time.monotonic()
    ok = signed_in(c)
    took = round(time.monotonic() - t0, 2)
    step("Redis hung: still signed in, quickly", ok and took < 3, seconds=took)
    sh(f"docker unpause {CONTAINER}")
    sh(f"docker stop {CONTAINER}")
    fresh = client(login=True)
    step(
        "Redis stopped: still signed in, new sign-in works",
        signed_in(c) and signed_in(fresh),
    )
    sh(f"docker start {CONTAINER}")
    time.sleep(1)

    # A cookie copied before a sign-out Redis didn't see: sign-out in one worker while Redis is
    # hung (it keeps its copy), reuse in another once it's back.
    key = sub("login")[0]
    sh(f"docker pause {CONTAINER}")
    status, row_left = sub("logout", key)
    sh(f"docker unpause {CONTAINER}")
    stale, reused = sub("probe", key)
    step(
        "copied cookie after a sign-out Redis missed is refused",
        status in ("200", "302")
        and row_left == "False"
        and stale == "True"
        and reused == "False",
        redis_held_old_copy=stale,
        signed_in=reused,
    )

    sh(f"docker rm -f {CONTAINER}")
    sh(f"docker run -d --name {CONTAINER} {RUN_ARGS} {IMAGE}")
    time.sleep(2)
    c = client(login=True)
    step("after container recreation (empty Redis)", signed_in(c))
    k = key_of(c)
    c.post("/auth/logout/")
    step(
        "sign-out: gone from PostgreSQL and Redis",
        not Session.objects.filter(pk=k).exists() and cached(k) is False,
    )
    print("ALL PASS" if all(results) else "SOME FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--phase":
        phase(*sys.argv[2:])
    else:
        sys.exit(main())
