"""Ambient per-request id, readable from anywhere (including Celery tasks).

``RequestIdMiddleware`` sets it for the lifetime of an HTTP request; the Celery
hooks in ``apps.core.celery_utils`` carry it across task boundaries so log rows
written by a worker can be traced back to the API call that triggered them.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)


def get_request_id() -> str:
    return _request_id.get()


def set_request_id(value: str) -> contextvars.Token:
    return _request_id.set(value or "")


def reset_request_id(token: contextvars.Token) -> None:
    try:
        _request_id.reset(token)
    except (ValueError, LookupError):
        pass


def new_request_id() -> str:
    return uuid.uuid4().hex


# ---- reads remembered while one page renders ------------------------------------------------
# A page asks the same small questions many times (entitlement in the nav, gates and the page;
# site settings; the workspace's timezone). ``RequestMemoMiddleware`` opens a memo for GET and
# HEAD requests; ``remembered`` reads each answer once inside it and every time outside it
# (POSTs, Celery, shells, tests that don't go through a request). A save that changes an answer
# calls ``forget``. Never the shared cache: each request starts from the database.
_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "request_memo", default=None
)


@contextmanager
def request_memo():
    token = _memo.set({})
    try:
        yield
    finally:
        _memo.reset(token)


def remembered(key: tuple, load: Callable[[], Any]) -> Any:
    memo = _memo.get()
    if memo is None:
        return load()
    if key not in memo:
        memo[key] = load()
    return memo[key]


def forget(kind: str | None = None) -> None:
    """Drop remembered answers whose key starts with ``kind`` (all of them when None)."""
    memo = _memo.get()
    if not memo:
        return
    for key in [k for k in memo if kind is None or k[0] == kind]:
        del memo[key]
