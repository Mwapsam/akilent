"""Customer media (photos, videos, voice notes, files) for the inbox, any channel.

The bytes live on the channel record (``whatsapp.MessageLog`` or
``instagram.InstagramMessage``), downloaded by that channel's media task. This
module only answers "what should the inbox show for this message" and "which
stored file is it", so the inbox and the media view never reason per channel.

Files are served through ``conversations:message_media`` (login + account
check), never by a public storage URL: they are customers' private messages.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models.fields.files import FieldFile
from django.urls import reverse

# How many download attempts before a file is reported unavailable (both
# channels' media tasks stop retrying at 5).
_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class StoredMedia:
    file: FieldFile
    mime: str


def _source(message):
    """The channel record holding this message's media fields, or None."""
    wa = getattr(message, "whatsapp_message", None)
    if wa is not None and (wa.media_id or wa.media_file):
        return {
            "file": wa.media_file,
            "mime": wa.media_mime_type or "",
            "hint": wa.message_type,
            "attempts": wa.media_attempts,
        }
    ig = getattr(message, "instagram_message", None)
    if ig is not None and (ig.media_source_url or ig.media_file):
        return {
            "file": ig.media_file,
            "mime": ig.media_mime_type or "",
            "hint": ig.media_type,
            "attempts": ig.media_attempts,
        }
    return None


def _kind(mime: str, hint: str) -> str:
    """image / video / audio / file — from the mime type, else the channel's own label."""
    major = (mime or "").split("/", 1)[0]
    if major in {"image", "video", "audio"}:
        return major
    if hint in {"image", "sticker", "story_mention"}:
        return "image"
    if hint in {"video", "audio"}:
        return hint
    return "file"


def media_json(message, conversation_public_id: str) -> dict | None:
    """What the inbox needs to show a message's media, or None if it has none.

    ``state`` is ``ready`` (``url`` set), ``pending`` (still downloading) or
    ``unavailable`` (the download gave up).
    """
    source = _source(message)
    if source is None:
        return None
    out = {"kind": _kind(source["mime"], source["hint"]), "mime": source["mime"]}
    if source["file"]:
        out["state"] = "ready"
        out["url"] = reverse(
            "conversations:message_media",
            args=[conversation_public_id, message.pk],
        )
    elif source["attempts"] >= _MAX_ATTEMPTS:
        out["state"] = "unavailable"
    else:
        out["state"] = "pending"
    return out


def stored_media(message) -> StoredMedia | None:
    """The downloaded file for this message, if there is one."""
    source = _source(message)
    if source is None or not source["file"]:
        return None
    return StoredMedia(file=source["file"], mime=source["mime"])
