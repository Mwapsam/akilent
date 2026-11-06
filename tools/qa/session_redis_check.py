"""Sessions against a real Redis: restart, flush, stop, hang, recreate, and a copied cookie after
a sign-out Redis didn't see (apps/core/sessions.py, docs/design/ui-quality-log.md Entry 24-25).

Run it where `docker` works (the host, not inside a container). Each sign-in, sign-out and
check runs as its own process through APP_EXEC, like separate workers; on a compose server
that's the web container. From the compose project folder on staging:

    QA_SESSION_USER=someone@example.com \\
    APP_EXEC="docker compose exec -T web python" \\
    REDIS_CONTAINER=$(docker compose ps -q redis) \\
    REDIS_RECREATE="docker compose up -d --force-recreate redis" \\
        python3 tools/qa/session_redis_check.py

It signs that user in and out, restarts, pauses, stops and recreates REDIS_CONTAINER, and clears
the sessions cache (its whole Redis database: with SESSION_REDIS_URL unset that's the app's
cache too). Use a test user and a server where that's acceptable. Each step prints PASS or FAIL.

Locally (Docker Redis started with `docker run --name akilent-uiqa-redis ...`) the defaults
work: APP_EXEC is this Python, and recreation is `docker rm -f` then `docker run`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

# Relative to the project folder, on the host and in the web container alike.
SCRIPT = "tools/qa/session_redis_check.py"
APP_EXEC = os.environ.get("APP_EXEC", shlex.quote(sys.executable))
CONTAINER = os.environ.get("REDIS_CONTAINER", "akilent-uiqa-redis")
IMAGE = os.environ.get("REDIS_IMAGE", "redis:7.4-alpine")
RUN_ARGS = os.environ.get("REDIS_RUN_ARGS", "-p 127.0.0.1:6379:6379")
RECREATE = os.environ.get("REDIS_RECREATE", "")


# --- Inside the app: one step per process ---------------------------------------------------


def phase(name: str, arg: str = "") -> None:
    sys.path.insert(0, str(Path.cwd()))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "automator.settings")
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth.models import User
    from django.contrib.sessions.models import Session
    from django.core.cache import caches
    from django.test import Client

    from apps.core.sessions import SessionStore

    settings.ALLOWED_HOSTS = ["*"]
    cache = caches[settings.SESSION_CACHE_ALIAS]

    def client(key):
        c = Client()
        c.cookies[settings.SESSION_COOKIE_NAME] = key
        return c

    def cached(key):
        try:
            return cache.get(SessionStore.cache_key_prefix + key) is not None
        except Exception as e:
            return f"unreachable({type(e).__name__})"

    if name == "login":  # arg: the user's email
        c = Client()
        c.force_login(User.objects.get(email__iexact=arg))
        c.get("/auth/login/")
        print(c.cookies[settings.SESSION_COOKIE_NAME].value)
    elif name == "probe":  # arg: a session key
        held = cached(arg)
        t0 = time.monotonic()
        ok = client(arg).get("/auth/login/").wsgi_request.user.is_authenticated
        print(held, ok, round(time.monotonic() - t0, 2))
    elif name == "logout":
        r = client(arg).post("/auth/logout/")
        print(r.status_code, Session.objects.filter(pk=arg).exists())
    elif name == "flush":
        cache.clear()
        print("cleared")


# --- On the host: Docker, and the steps in order --------------------------------------------


def sh(cmd: str) -> str:
    """A docker command; failures are read from the output, not raised."""
    r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=False)  # noqa: S603 - our own docker commands
    return (r.stdout + r.stderr).strip()


def app(*args: str) -> list[str]:
    """One step in the app, as its own process; its last line of output, split."""
    cmd = [*shlex.split(APP_EXEC), SCRIPT, "--phase", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603 - this script, in the app
    lines = r.stdout.strip().splitlines()
    if r.returncode or not lines:
        sys.exit(f"App step {args[0]} failed:\n{r.stderr.strip()[-2000:]}")
    return lines[-1].split()


def main() -> int:
    email = os.environ.get("QA_SESSION_USER", "")
    if not email:
        sys.exit(
            "Set QA_SESSION_USER to a test user's email (see the top of this file)."
        )
    state = sh(f"docker inspect -f {{{{.State.Status}}}} {CONTAINER}")
    if state != "running":
        sys.exit(
            f"Redis container {CONTAINER!r} isn't running here ({state}). Run this on the "
            "Docker host with REDIS_CONTAINER set, not inside a container."
        )

    results = []

    def step(name, ok, **info):
        results.append(bool(ok))
        print("PASS" if ok else "FAIL", name, info or "")

    def probe(key):
        held, signed_in, seconds = app("probe", key)
        return held, signed_in == "True", float(seconds)

    key = app("login", email)[0]
    held, ok, _ = probe(key)
    step("signed in, session in Redis", ok and held == "True", redis=held)

    sh(f"docker restart {CONTAINER}")
    step("after Redis restart", probe(key)[1])

    app("flush")
    _, ok, _ = probe(key)  # read from PostgreSQL, which puts it back in Redis
    held = probe(key)[0]
    step(
        "after flush: from PostgreSQL, cache refilled",
        ok and held == "True",
        redis=held,
    )

    sh(f"docker pause {CONTAINER}")
    _, ok, took = probe(key)
    sh(f"docker unpause {CONTAINER}")
    step("Redis hung: still signed in, quickly", ok and took < 3, seconds=took)

    sh(f"docker stop {CONTAINER}")
    fresh = app("login", email)[0]
    step(
        "Redis stopped: still signed in, new sign-in works",
        probe(key)[1] and probe(fresh)[1],
    )
    sh(f"docker start {CONTAINER}")
    time.sleep(2)

    # A cookie copied before a sign-out Redis didn't see: sign out while Redis is hung (it keeps
    # its copy), then present the cookie once it's back.
    copied = app("login", email)[0]
    sh(f"docker pause {CONTAINER}")
    status, row_left = app("logout", copied)
    sh(f"docker unpause {CONTAINER}")
    held, reused, _ = probe(copied)
    step(
        "copied cookie after a sign-out Redis missed is refused",
        status in ("200", "302")
        and row_left == "False"
        and held == "True"
        and not reused,
        redis_held_old_copy=held,
        signed_in=reused,
    )

    if RECREATE:
        sh(RECREATE)
    else:
        sh(f"docker rm -f {CONTAINER}")
        sh(f"docker run -d --name {CONTAINER} {RUN_ARGS} {IMAGE}")
    time.sleep(5)
    key = app("login", email)[0]
    step("after container recreation (empty Redis)", probe(key)[1])

    status, row_left = app("logout", key)
    held, ok, _ = probe(key)
    step(
        "sign-out: gone from PostgreSQL and Redis",
        row_left == "False" and held == "False" and not ok,
        redis=held,
    )
    print("ALL PASS" if all(results) else "SOME FAILED")
    return 0 if all(results) else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--phase":
        phase(*sys.argv[2:])
    else:
        sys.exit(main())
