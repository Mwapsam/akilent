"""Reading pictures safely, in one place: the bounded download and the check that the bytes
really are a picture. Used by the CSV import, stock photos, the picture editor and the bio
page, so a fix here (a size cap, decompression-bomb handling) reaches all of them."""

from __future__ import annotations

import io

IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_CHUNK = 64 * 1024


class ImageError(ValueError):
    """The picture can't be used; the message is for the person."""


def verify(data: bytes) -> None:
    """Raise ``ImageError`` unless ``data`` is a picture Pillow can read. Pillow refuses
    images big enough to be decompression bombs."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ImageError("That isn't a picture we can read.") from exc


def download(resp, max_bytes: int, label: str) -> tuple[bytes, str]:
    """Read a streamed HTTP response (``stream=True``) as a picture: it must answer 200
    with a JPEG, PNG or WebP type, stay under ``max_bytes`` and verify. Returns
    ``(data, mime)`` and always closes ``resp``. ``label`` names it in messages."""
    try:
        if resp.status_code != 200:
            raise ImageError(f"{label} answered {resp.status_code}.")
        mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if mime not in IMAGE_TYPES:
            raise ImageError(f"{label} isn't a JPEG, PNG or WebP image.")
        body = bytearray()
        for chunk in resp.iter_content(_CHUNK):
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ImageError(f"{label} is over {max_bytes // (1024 * 1024)} MB.")
    finally:
        resp.close()
    data = bytes(body)
    try:
        verify(data)
    except ImageError as exc:
        raise ImageError(f"{label} isn't a picture we can read.") from exc
    return data, mime
