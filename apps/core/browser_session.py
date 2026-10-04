"""Ambient per-request browser session id, readable from anywhere.

``BrowserSessionMiddleware`` sets it for the lifetime of an HTTP request via
the public API below. Never reach into ``_var`` directly from outside this
module — the token-based reset pattern requires the encapsulation to stay intact
so that concurrency tests are meaningful.
"""

from __future__ import annotations

import contextvars

_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "browser_session_id", default=""
)


def get_browser_session_id() -> str:
    return _var.get()


def set_browser_session_id(value: str) -> contextvars.Token:
    """Store value; returns a token that the caller MUST pass to reset_browser_session_id()."""
    return _var.set(value or "")


def reset_browser_session_id(token: contextvars.Token) -> None:
    """Restore the previous value.  Always call in a finally block."""
    try:
        _var.reset(token)
    except (ValueError, LookupError):
        pass
