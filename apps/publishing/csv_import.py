"""Bulk import from CSV (Phase 2b, ``cs_csv_import``): rows become **drafts only**.

1. ``preview`` reads the file (at most ``MAX_ROWS`` rows and ``MAX_BYTES``), checks each
   row (its columns, its media, then the composer's own readiness check) and lists
   what's wrong per row. Nothing is saved.
2. ``import_rows`` saves the rows without errors as drafts through the composer's save
   path. A draft is never scheduled or published: each one is opened, reviewed if the
   workspace reviews posts, and scheduled by a person.

Media must be on our own media host, or a public ``https`` image that is copied in.
Fetching goes through the knowledge importer's guard (``apps.ai.knowledge``): every
address the host resolves to must be public, the connection is pinned to the checked
address, and redirects aren't followed (SSRF). Videos must be uploaded in the composer,
where they are checked.

Columns, in any order with a header row: ``caption``, ``platforms`` (instagram and/or
facebook, separated by ``;`` or ``|``), ``media_type``, ``media_urls`` (separated by
spaces or ``;``), ``scheduled_at`` (``YYYY-MM-DD HH:MM`` on the business's clock, kept
on the draft as the suggested time), ``hashtags``.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import uuid

from django.utils import timezone

from .models import ContentPost

logger = logging.getLogger(__name__)

FLAG = "cs_csv_import"
MAX_ROWS = 100
ROW_KEY = "_key"  # added to each row by read_file; not a column
MAX_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # Instagram's image limit
COLUMNS = (
    "caption",
    "platforms",
    "media_type",
    "media_urls",
    "scheduled_at",
    "hashtags",
)
PLATFORMS = ("instagram", "facebook")
_SPLIT = re.compile(r"[;|,\s]+")
# Media URLs can contain commas (e.g. image CDN transforms), so they split on spaces and ";".
_URL_SPLIT = re.compile(r"[;\s]+")


class CsvError(ValueError):
    """The file can't be read; the message is for the person."""


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def read_file(upload) -> list[dict]:
    """The file's rows as {column: value}. Raises CsvError for the whole file."""
    if upload is None:
        raise CsvError("Choose a CSV file.")
    if upload.size > MAX_BYTES:
        raise CsvError("The file is over 1 MB. Split it into smaller files.")
    try:
        text = upload.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError("Save the file as CSV (UTF-8) and try again.") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvError("The file is empty.")
    names = {(n or "").strip().lower() for n in reader.fieldnames}
    if not names & {"caption", "media_urls"}:
        raise CsvError(
            "The first row must name the columns, such as caption, platforms, "
            "media_type, media_urls."
        )
    rows = []
    for raw in reader:
        row = {
            (k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k
        }
        if not any(row.get(c) for c in COLUMNS):
            continue  # a blank line
        # Each row's own key becomes its draft's client_key, so importing the same
        # preview twice (a timeout, a double click) never makes a second draft.
        row[ROW_KEY] = str(uuid.uuid4())
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise CsvError(f"Import up to {MAX_ROWS} posts at a time.")
    if not rows:
        raise CsvError("The file has no posts in it.")
    return rows


def _media_problem(url: str) -> str:
    """Why ``url`` can't be used, or "" when it can."""
    from apps.ai.knowledge import WebsiteError, _public_ip

    from .services import _is_own_media_url

    if _is_own_media_url(url):
        return ""
    if not url.lower().startswith("https://"):
        return f"Use an https address for {url[:80]}."
    from .constraints import media_kind

    if media_kind(url) == "video":
        return "Upload videos in the composer, where they're checked."
    try:
        _public_ip(url)
    except WebsiteError as exc:
        return f"{url[:80]}: {exc}"
    return ""


def _parse_row(row: dict):
    """``(ComposerInput, errors)`` for one row."""
    from .composer import ComposerInput, parse_schedule

    errors = []
    platforms = [p.lower() for p in _SPLIT.split(row.get("platforms", "")) if p]
    unknown = [p for p in platforms if p not in PLATFORMS]
    if unknown:
        errors.append(f"Unknown channel: {', '.join(unknown)}.")
    platforms = [p for p in PLATFORMS if p in platforms] or []
    if not platforms:
        errors.append("Name a channel: instagram, facebook or both.")
    urls = [u for u in _URL_SPLIT.split(row.get("media_urls", "")) if u]
    media_type = (row.get("media_type") or "").lower() or (
        ContentPost.MediaType.IMAGE if urls else ContentPost.MediaType.TEXT
    )
    if media_type not in ContentPost.MediaType.values:
        errors.append(f"Unknown media type: {media_type}.")
        media_type = ContentPost.MediaType.IMAGE
    for url in urls:
        problem = _media_problem(url)
        if problem:
            errors.append(problem)
    when = None
    if row.get("scheduled_at"):
        when = parse_schedule(row["scheduled_at"].replace(" ", "T", 1))
        if when is None:
            errors.append("Write the time as YYYY-MM-DD HH:MM.")
        elif when <= timezone.now():
            errors.append("The suggested time has passed.")
    inp = ComposerInput(
        caption=row.get("caption", ""),
        hashtags=[t.lstrip("#") for t in _SPLIT.split(row.get("hashtags", "")) if t],
        platforms=platforms,
        media_type=media_type,
        media_urls=urls,
        scheduled_at=when,
    )
    return inp, errors


def preview(account, rows: list[dict]) -> list[dict]:
    """Each row with what's wrong with it. Nothing is saved."""
    from . import readiness

    out = []
    for n, row in enumerate(rows, 1):
        inp, errors = _parse_row(row)
        # Nothing is saved by a preview, so no media checks are started either.
        issues = [] if errors else readiness.check(inp, account, start_checks=False)
        out.append(
            {
                "n": n,
                "row": row,
                "caption": inp.caption,
                "platforms": inp.platforms,
                "media_type": inp.media_type,
                "media_count": len(inp.media_urls),
                "scheduled_at": inp.scheduled_at,
                "errors": errors,
                # What still has to be fixed before it can be scheduled, once a draft.
                "to_fix": [
                    i["message"] for i in readiness.blocking(issues, readiness.SCHEDULE)
                ],
                "ok": not errors,
            }
        )
    return out


def _copy_in(account, url: str) -> str:
    """Copy a public image into our media storage and return its URL there."""
    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    from apps.ai.knowledge import _open, _public_ip

    from . import images
    from .services import _is_own_media_url

    if _is_own_media_url(url):
        return url
    resp = _open(url, ip=_public_ip(url), accept="image/*")
    data, kind = images.download(resp, MAX_IMAGE_BYTES, url[:80])
    ext = images.IMAGE_TYPES[kind]
    path = default_storage.save(
        f"publishing/{account.pk}/import/{uuid.uuid4().hex}.{ext}", ContentFile(data)
    )
    stored = default_storage.url(path)
    from .constraints import fb_formats_released

    if fb_formats_released():
        from .media_checks import record_upload

        record_upload(account, stored, data, kind)
    from .media_library import name_from_url, record_if_released

    record_if_released(
        account,
        stored,
        kind="image",
        mime=kind,
        name=name_from_url(url)[:200],
        data=data,
        source="import",
    )
    return stored


def import_rows(account, user, rows: list[dict]) -> dict:
    """Save each row without errors as a draft. ``{created: [post], failed: [(n, why)]}``.
    Rows are read again here; nothing from the preview is trusted."""
    from django.http import QueryDict

    from .composer import parse_client_key, save_new_or_existing

    created, failed = [], []
    for n, row in enumerate(rows, 1):
        key = parse_client_key(row.get(ROW_KEY))
        if key and ContentPost.objects.filter(account=account, client_key=key).exists():
            continue  # imported already
        inp, errors = _parse_row(row)
        if errors:
            failed.append((n, " ".join(errors)))
            continue
        try:
            inp.media_urls = [_copy_in(account, u) for u in inp.media_urls]
        except Exception as exc:
            logger.info("CSV import media failed row=%s: %s", n, exc)
            failed.append((n, f"Couldn't copy the media: {exc}"))
            continue
        post = ContentPost(account=account, created_by=user, client_key=key)
        result = save_new_or_existing(post, inp, "draft", user, QueryDict())
        created.append(result.post)
    return {"created": created, "failed": failed}
