"""Machine-readable API changelog.

Newest entry first. Each entry is append-only once shipped; edit the prose
mirror at ``templates/docs/changelog.html`` to match. ``type`` is one of
``added`` | ``changed`` | ``deprecated`` | ``fixed``. All v1 changes are
backwards compatible (additive) by policy — anything breaking accrues to v2.
"""
from __future__ import annotations

CHANGELOG: list[dict] = [
    {
        "date": "2026-09-10",
        "version": "v1",
        "changes": [
            {"type": "added", "summary": "GET /v1/analytics — grouped rollups by day, template, campaign, or domain."},
            {"type": "added", "summary": "Workflows API: CRUD, publish/archive, enrol contacts, inspect runs."},
            {"type": "added", "summary": "Workflow triggers for contact.created/updated and email.opened/clicked."},
            {"type": "added", "summary": "GET /v1/changelog — this feed."},
        ],
    },
    {
        "date": "2026-09-03",
        "version": "v1",
        "changes": [
            {"type": "added", "summary": "GET /v1/templates/{slug}/versions and POST .../versions/{n}/activate for rollback."},
            {"type": "added", "summary": "GET /v1/version — machine-readable version metadata."},
        ],
    },
    {
        "date": "2026-08-27",
        "version": "v1",
        "changes": [
            {"type": "added", "summary": "GET /v1/deliverability — composite score, checks, and recommendations."},
            {"type": "added", "summary": "Contacts, lists, and segments API (Phase 4)."},
            {"type": "added", "summary": "POST /v1/events — business-event ingestion, idempotent."},
            {"type": "added", "summary": "POST /v1/webhooks/test — fan a synthetic event through signed delivery."},
        ],
    },
    {
        "date": "2026-08-20",
        "version": "v1",
        "changes": [
            {"type": "added", "summary": "OpenAPI schema at /api/schema; Swagger at /api/docs; Redoc at /api/reference."},
            {"type": "added", "summary": "Test-mode API keys (ak_test_) with the sandbox provider and magic recipients."},
            {"type": "added", "summary": "Idempotency-Key support on POST /v1/messages and /v1/campaigns."},
            {"type": "added", "summary": "GET /v1/messages, /v1/messages/{id}, /v1/messages/{id}/events, /v1/request-logs."},
            {"type": "changed", "summary": "Error envelope now carries request_id and docs_url."},
        ],
    },
]
