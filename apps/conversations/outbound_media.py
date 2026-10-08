"""Media a team member sends from the inbox: photos, videos, documents, voice notes.

Turns an uploaded file into something the conversation's channel will accept,
stores it, and hands back where it is. Channel rules live here, once:

* WhatsApp takes images (jpeg/png), mp4 video, documents, and audio — a voice
  note must be Ogg/Opus to arrive as a voice note.
* Instagram takes images, mp4 video, aac/m4a audio, and PDFs as files.

Browsers record voice in webm/opus (Chrome), ogg/opus (Firefox) or mp4/aac
(Safari), so recordings are converted with ffmpeg where the channel needs it.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

WHATSAPP = "whatsapp"
INSTAGRAM = "instagram"

_MB = 1024 * 1024
# Meta's own ceilings per kind (the smaller of the two channels where they differ).
_MAX_BYTES = {
    WHATSAPP: {
        "image": 5 * _MB,
        "video": 16 * _MB,
        "audio": 16 * _MB,
        "document": 16 * _MB,
    },
    INSTAGRAM: {
        "image": 8 * _MB,
        "video": 25 * _MB,
        "audio": 25 * _MB,
        "document": 25 * _MB,
    },
}
_IMAGE_TYPES = {
    WHATSAPP: {"image/jpeg", "image/png"},
    INSTAGRAM: {"image/jpeg", "image/png", "image/gif"},
}
_VIDEO_TYPES = {"video/mp4", "video/3gpp"}
# Audio each channel takes as-is.
_AUDIO_OK = {
    WHATSAPP: {"audio/ogg", "audio/aac", "audio/mp4", "audio/mpeg", "audio/amr"},
    INSTAGRAM: {"audio/aac", "audio/mp4", "audio/x-m4a", "audio/wav"},
}
_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "audio/ogg": ".ogg",
    "audio/aac": ".aac",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/amr": ".amr",
    "audio/wav": ".wav",
    "application/pdf": ".pdf",
}


class MediaError(Exception):
    """The file can't be sent on this channel; the message says why, for a person."""


@dataclass(frozen=True)
class PreparedMedia:
    path: str  # storage path
    mime: str
    kind: str  # image / video / audio / document
    filename: str


def prepare(
    upload, channel: str, *, account_id: int, voice: bool = False
) -> PreparedMedia:
    """Validate, convert if needed, and store ``upload`` for sending on ``channel``."""
    mime = _base_mime(getattr(upload, "content_type", "") or "")
    content = upload.read()
    if not content:
        raise MediaError("That file is empty.")
    filename = Path(getattr(upload, "name", "") or "file").name[:100]

    kind = _kind(mime, channel, voice)
    if kind == "audio" and mime not in _AUDIO_OK[channel]:
        content, mime = _convert_audio(content, channel)
    elif kind == "audio" and voice and channel == WHATSAPP and mime != "audio/ogg":
        # Only Ogg/Opus shows as a playable voice note on WhatsApp.
        content, mime = _convert_audio(content, channel)

    if kind == "document" and channel == INSTAGRAM and mime != "application/pdf":
        raise MediaError("Instagram can only send PDF documents.")

    limit = _MAX_BYTES[channel][kind]
    if len(content) > limit:
        raise MediaError(
            f"That {kind} is too large to send — the limit is {limit // _MB} MB."
        )

    ext = _EXT.get(mime) or Path(filename).suffix[:10] or ".bin"
    path = default_storage.save(
        f"outbound/{account_id}/{uuid.uuid4().hex}{ext}", ContentFile(content)
    )
    if voice:
        filename = f"voice-note{ext}"
    return PreparedMedia(path=path, mime=mime, kind=kind, filename=filename)


def _base_mime(mime: str) -> str:
    return mime.split(";", 1)[0].strip().lower()


def _kind(mime: str, channel: str, voice: bool) -> str:
    # A recording is audio even when the browser labels it video/webm.
    if voice or mime.startswith("audio/"):
        return "audio"
    if mime in _IMAGE_TYPES[channel]:
        return "image"
    if mime.startswith("image/"):
        raise MediaError("Send photos as JPG or PNG.")
    if mime in _VIDEO_TYPES:
        return "video"
    if mime.startswith("video/"):
        raise MediaError("Send videos as MP4.")
    return "document"


def _convert_audio(content: bytes, channel: str) -> tuple[bytes, str]:
    """Re-encode audio to Ogg/Opus (WhatsApp voice note) or M4A/AAC (Instagram)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaError(
            "Voice notes can't be converted on this server yet (ffmpeg is missing)."
        )
    if channel == WHATSAPP:
        out_ext, out_mime = ".ogg", "audio/ogg"
        codec = ["-c:a", "libopus", "-b:a", "32k", "-ac", "1"]
    else:
        out_ext, out_mime = ".m4a", "audio/mp4"
        codec = ["-c:a", "aac", "-b:a", "64k", "-ac", "1"]
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in"
        dst = Path(tmp) / f"out{out_ext}"
        src.write_bytes(content)
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell, our own temp paths
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(src),
                "-vn",
                *codec,
                str(dst),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0 or not dst.exists():
            raise MediaError("We couldn't process that recording. Try recording again.")
        return dst.read_bytes(), out_mime
