"""Parsing, validation, and transport encoding for message attachments.

The public API accepts ``[{filename, content_b64, content_type?}]``. Content is
base64 in / base64 out so it survives JSON (the request body and the Celery task
kwargs). Providers receive decoded ``apps.email.types.Attachment`` objects.
"""
from __future__ import annotations

import base64
import binascii

from apps.email.types import Attachment

MAX_FILE_BYTES = 5 * 1024 * 1024        # 5 MiB per file
MAX_TOTAL_BYTES = 10 * 1024 * 1024      # 10 MiB across all attachments
MAX_COUNT = 10


class AttachmentError(ValueError):
    """A caller-supplied attachment failed validation."""


def parse_attachments(raw) -> list[Attachment]:
    """Validate + decode a list of ``{filename, content_b64, content_type?}`` dicts."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise AttachmentError("attachments must be a list")
    if len(raw) > MAX_COUNT:
        raise AttachmentError(f"at most {MAX_COUNT} attachments are allowed")

    out: list[Attachment] = []
    total = 0
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AttachmentError(f"attachments[{i}] must be an object")
        filename = (item.get("filename") or "").strip()
        if not filename:
            raise AttachmentError(f"attachments[{i}] is missing a filename")
        b64 = item.get("content_b64") or item.get("content")
        if not b64:
            raise AttachmentError(f"attachments[{i}] ({filename}) is missing content_b64")
        try:
            content = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AttachmentError(f"attachments[{i}] ({filename}) is not valid base64") from exc
        if len(content) > MAX_FILE_BYTES:
            raise AttachmentError(
                f"attachment {filename} is {len(content)} bytes; the limit is {MAX_FILE_BYTES}"
            )
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise AttachmentError(
                f"attachments total {total} bytes; the limit is {MAX_TOTAL_BYTES}"
            )
        out.append(Attachment(
            filename=filename,
            content=content,
            content_type=(item.get("content_type") or "application/octet-stream").strip(),
        ))
    return out


def attachments_metadata(attachments) -> list[dict]:
    """Small dicts for EmailMessage.attachments (no content) — for the logs UI."""
    return [
        {"filename": a.filename, "content_type": a.content_type, "size": len(a.content)}
        for a in attachments
    ]


def encode_for_task(attachments) -> list[dict]:
    """Re-encode decoded Attachments as JSON-safe dicts for Celery task kwargs."""
    return [
        {
            "filename": a.filename,
            "content_type": a.content_type,
            "content_b64": base64.b64encode(a.content).decode("ascii"),
        }
        for a in attachments
    ]


def decode_from_task(raw) -> list[Attachment]:
    """Inverse of :func:`encode_for_task` — trusted input, no size re-check."""
    out: list[Attachment] = []
    for item in raw or []:
        out.append(Attachment(
            filename=item["filename"],
            content=base64.b64decode(item["content_b64"]),
            content_type=item.get("content_type", "application/octet-stream"),
        ))
    return out
