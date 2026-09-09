"""Ambient per-request id, readable from anywhere (including Celery tasks).

``RequestIdMiddleware`` sets it for the lifetime of an HTTP request; the Celery
hooks in ``apps.core.celery_utils`` carry it across task boundaries so log rows
written by a worker can be traced back to the API call that triggered them.
"""
from __future__ import annotations

import contextvars
import uuid

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


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
