"""Machine-readable API changelog.

Newest entry first. Each entry is append-only once shipped; edit the prose
mirror at ``templates/docs/changelog.html`` to match. ``type`` is one of
``added`` | ``changed`` | ``deprecated`` | ``fixed``. All v1 changes are
backwards compatible (additive) by policy — anything breaking accrues to v2.
"""

from __future__ import annotations

CHANGELOG: list[dict] = [
    {
        "date": "2026-10-21",
        "version": "v1",
        "changes": [
            {
                "type": "added",
                "summary": "WhatsApp campaigns API: GET/POST /v1/whatsapp/campaigns, GET /v1/whatsapp/campaigns/{id}, POST .../{id}/pause|resume|cancel|send_now (scope whatsapp:campaigns, Idempotency-Key).",
            },
            {
                "type": "added",
                "summary": "POST /v1/contacts accepts phone without email; contacts include phone, whatsapp, tags, lists, lifecycle_stage; PATCH sets whatsapp opt-in (attested) or opt-out.",
            },
            {
                "type": "added",
                "summary": "GET/PATCH/DELETE /v1/lists/{id}; POST/DELETE /v1/lists/{id}/members (up to 1,000 by contact id or phone).",
            },
            {
                "type": "added",
                "summary": "Segment fields whatsapp, whatsapp_reachable, has_phone, has_order, first_seen_days, phone, lifecycle_stage.",
            },
            {
                "type": "changed",
                "summary": "DELETE /v1/contacts/{id} also removes the customer's WhatsApp and Instagram identities and archives a linked HubSpot contact.",
            },
        ],
    },
    {
        "date": "2026-09-10",
        "version": "v1",
        "changes": [
            {
                "type": "added",
                "summary": "GET /v1/analytics — grouped rollups by day, template, campaign, or domain.",
            },
            {
                "type": "added",
                "summary": "Workflows API: CRUD, publish/archive, enrol contacts, inspect runs; starter templates at GET /v1/workflows/templates.",
            },
            {
                "type": "added",
                "summary": "Workflow triggers for contact.created/updated and email.opened/clicked.",
            },
            {
                "type": "added",
                "summary": "POST /v1/messages accepts `locale` and `attachments[]` (base64, 5 MiB/file, 10 MiB total).",
            },
            {
                "type": "added",
                "summary": "Template localization: GET /v1/templates/{slug}/locales, PUT/DELETE .../locales/{locale}.",
            },
            {
                "type": "added",
                "summary": "Template render-time data sources: GET/PUT/DELETE /v1/templates/{slug}/data-sources[/{key}].",
            },
            {
                "type": "added",
                "summary": "GET /v1/campaigns/{id}/versions — content snapshots.",
            },
            {"type": "added", "summary": "GET /v1/changelog — this feed."},
        ],
    },
    {
        "date": "2026-09-03",
        "version": "v1",
        "changes": [
            {
                "type": "added",
                "summary": "GET /v1/templates/{slug}/versions and POST .../versions/{n}/activate for rollback.",
            },
            {
                "type": "added",
                "summary": "GET /v1/version — machine-readable version metadata.",
            },
        ],
    },
    {
        "date": "2026-08-27",
        "version": "v1",
        "changes": [
            {
                "type": "added",
                "summary": "GET /v1/deliverability — composite score, checks, and recommendations.",
            },
            {
                "type": "added",
                "summary": "Contacts, lists, and segments API (Phase 4).",
            },
            {
                "type": "added",
                "summary": "POST /v1/events — business-event ingestion, idempotent.",
            },
            {
                "type": "added",
                "summary": "POST /v1/webhooks/test — fan a synthetic event through signed delivery.",
            },
        ],
    },
    {
        "date": "2026-08-20",
        "version": "v1",
        "changes": [
            {
                "type": "added",
                "summary": "OpenAPI schema at /api/schema; Swagger at /api/docs; Redoc at /api/reference.",
            },
            {
                "type": "added",
                "summary": "Test-mode API keys (ak_test_) with the sandbox provider and magic recipients.",
            },
            {
                "type": "added",
                "summary": "Idempotency-Key support on POST /v1/messages and /v1/campaigns.",
            },
            {
                "type": "added",
                "summary": "GET /v1/messages, /v1/messages/{id}, /v1/messages/{id}/events, /v1/request-logs.",
            },
            {
                "type": "changed",
                "summary": "Error envelope now carries request_id and docs_url.",
            },
        ],
    },
]
