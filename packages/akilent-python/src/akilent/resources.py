"""Resource namespaces exposed on the client (`client.messages`, …)."""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from typing import Any

from ._transport import Transport


class _Base:
    def __init__(self, transport: Transport) -> None:
        self._t = transport


class Messages(_Base):
    def send(
        self,
        *,
        from_: str,
        to: str,
        subject: str = "",
        text: str | None = None,
        html: str | None = None,
        template: str | None = None,
        variables: dict | None = None,
        locale: str | None = None,
        attachments: list | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"from": from_, "to": to}
        if subject:
            payload["subject"] = subject
        if text is not None:
            payload["text"] = text
        if html is not None:
            payload["html"] = html
        if template is not None:
            payload["template"] = template
        if variables is not None:
            payload["template_variables"] = variables
        if locale is not None:
            payload["locale"] = locale
        if attachments is not None:
            payload["attachments"] = attachments
        return self._t.request(
            "POST", "/api/v1/messages", json=payload, idempotency_key=idempotency_key
        )

    def list(self, **filters: Any) -> dict:
        return self._t.request("GET", "/api/v1/messages", params=filters or None)

    def retrieve(self, message_id: str) -> dict:
        return self._t.request("GET", f"/api/v1/messages/{message_id}")

    def events(self, message_id: str) -> builtins.list[dict]:
        return self._t.request("GET", f"/api/v1/messages/{message_id}/events")["data"]  # type: ignore[return-value]

    def iter(self, *, page_size: int = 100, **filters: Any) -> Iterator[dict]:
        offset = 0
        while True:
            page = self.list(limit=page_size, offset=offset, **filters)
            rows: list[dict] = page.get("data", [])
            yield from rows
            offset += len(rows)
            if not rows or offset >= page.get("total", 0):
                return


class Templates(_Base):
    def list(self) -> list:
        return self._t.request("GET", "/api/v1/templates")

    def create(self, **body: Any) -> dict:
        return self._t.request("POST", "/api/v1/templates", json=body)

    def retrieve(self, slug: str) -> dict:
        return self._t.request("GET", f"/api/v1/templates/{slug}")

    def update(self, slug: str, **body: Any) -> dict:
        return self._t.request("PATCH", f"/api/v1/templates/{slug}", json=body)

    def delete(self, slug: str) -> None:
        self._t.request("DELETE", f"/api/v1/templates/{slug}")

    def preview(self, slug: str, *, variables: dict | None = None) -> dict:
        return self._t.request(
            "POST",
            f"/api/v1/templates/{slug}/preview",
            json={"variables": variables or {}},
        )

    def clone(self, slug: str) -> dict:
        return self._t.request("POST", f"/api/v1/templates/{slug}/clone")


class Campaigns(_Base):
    def create(self, **body: Any) -> dict:
        return self._t.request("POST", "/api/v1/campaigns", json=body)

    def retrieve(self, campaign_id: int) -> dict:
        return self._t.request("GET", f"/api/v1/campaigns/{campaign_id}")


class RequestLogs(_Base):
    def list(self, **filters: Any) -> dict:
        return self._t.request("GET", "/api/v1/request-logs", params=filters or None)

    def retrieve(self, request_id: str) -> dict:
        return self._t.request("GET", f"/api/v1/request-logs/{request_id}")
