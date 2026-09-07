"""Internal debug MCP server for akilent.com.

Wraps apps.internal_debug's read-only debug API (plus webhook-event resend)
as MCP tools. Run locally by an engineer debugging the live deployment —
never deployed as part of the Django app itself.

Config (environment variables, not committed):
    TASKCENTRO_DEBUG_BASE_URL   default: https://akilent.com
    TASKCENTRO_DEBUG_TOKEN      required — must match settings.INTERNAL_DEBUG_TOKEN

Setup (run once):
    poetry add --group dev mcp   # requests is already a project dependency

Register with Claude Code:
    claude mcp add --transport stdio taskcentro_debug -- poetry run python tools/mcp_taskcentro_debug/server.py
"""
from __future__ import annotations

import os
import sys

import requests
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("TASKCENTRO_DEBUG_BASE_URL", "https://akilent.com").rstrip("/")
TOKEN = os.environ.get("TASKCENTRO_DEBUG_TOKEN", "")

if not TOKEN:
    print(
        "TASKCENTRO_DEBUG_TOKEN is not set — set it in the environment before "
        "running this server (must match the target deployment's "
        "INTERNAL_DEBUG_TOKEN).",
        file=sys.stderr,
    )

mcp = FastMCP("taskcentro-debug")


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _get(path: str, params: dict | None = None) -> dict:
    resp = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _post(path: str) -> dict:
    resp = requests.post(f"{BASE_URL}{path}", headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
def fetch_page(path: str) -> dict:
    """Fetch a page from akilent.com and return its status code + HTML.

    Public pages are fetched directly. Internal/staff pages (anything needing
    a logged-in session, e.g. dashboard views) are automatically routed
    through the internal debug API's server-side renderer instead.
    """
    internal_prefixes = ("/whatsapp/", "/manage/", "/billing/", "/settings/")
    if path.startswith(internal_prefixes):
        return _get("/internal/debug/api/page/", {"path": path})
    resp = requests.get(f"{BASE_URL}{path}", timeout=15)
    return {"status_code": resp.status_code, "content": resp.text}


@mcp.tool()
def list_whatsapp_numbers(account_id: int) -> dict:
    """List WhatsApp connection records for an account (tokens redacted to booleans)."""
    return _get("/internal/debug/api/whatsapp-numbers/", {"account_id": account_id})


@mcp.tool()
def list_webhook_events(
    phone_number_id: str | None = None,
    processed: bool | None = None,
    limit: int = 50,
) -> dict:
    """List recent WhatsApp webhook events, optionally filtered."""
    params: dict = {"limit": limit}
    if phone_number_id is not None:
        params["phone_number_id"] = phone_number_id
    if processed is not None:
        params["processed"] = str(processed)
    return _get("/internal/debug/api/webhook-events/", params)


@mcp.tool()
def get_onboarding_state(account_id: int) -> dict:
    """Get an account's onboarding checklist state."""
    return _get("/internal/debug/api/onboarding-state/", {"account_id": account_id})


@mcp.tool()
def resend_webhook_event(event_id: int) -> dict:
    """Re-trigger processing of an already-received webhook event by id.

    Replays already-verified local data — does not call out to Meta or send
    any outbound message.
    """
    return _post(f"/internal/debug/api/webhook-events/{event_id}/resend/")


if __name__ == "__main__":
    mcp.run()
