"""Instagram Content Publishing provider.

Wraps the Instagram Graph API container-based publishing flow:
  1. create_media_container() — POST /{ig-user-id}/media → container_id
  2. poll_container_status()  — GET /{container_id}?fields=status_code
  3. publish_container()      — POST /{ig-user-id}/media_publish → ig_media_id

Images: status is FINISHED immediately; publish straight away.
Videos/Reels/Stories: poll until FINISHED (async processing by Meta).

Requires scope: instagram_content_publish
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

from .errors import MetaPublishError

logger = logging.getLogger(__name__)

_GRAPH_VERSION = settings.INSTAGRAM_GRAPH_VERSION
_BASE = f"https://graph.instagram.com/{_GRAPH_VERSION}"

CONTAINER_STATUS_FINISHED = "FINISHED"
CONTAINER_STATUS_ERROR = "ERROR"
CONTAINER_STATUS_EXPIRED = "EXPIRED"


class InstagramPublishError(MetaPublishError):
    pass


_ERROR_HINTS: list[tuple[str, str]] = [
    (
        "aspect ratio",
        "Unsupported aspect ratio. Instagram accepts: square (1:1), portrait (4:5), "
        "landscape (1.91:1), and Reels/Stories (9:16). Resize your image and try again.",
    ),
    (
        "image size",
        "Image too large for Instagram. Maximum file size is 8 MB.",
    ),
    (
        "video duration",
        "Video duration is outside Instagram's limits (3 s - 60 s for feed; up to 15 min for Reels).",
    ),
]


def _friendly_error(api_message: str) -> str:
    lower = api_message.lower()
    for fragment, hint in _ERROR_HINTS:
        if fragment in lower:
            return hint
    return api_message


class InstagramPublishProvider:
    def __init__(self, access_token: str) -> None:
        self._token = access_token

    def _get(self, path: str, **params) -> dict:
        params["access_token"] = self._token
        r = requests.get(f"{_BASE}/{path}", params=params, timeout=30)
        data = r.json()
        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            raise InstagramPublishError.from_response(r, data, _friendly_error(msg))
        return data

    def _post(self, path: str, **payload) -> dict:
        payload["access_token"] = self._token
        r = requests.post(f"{_BASE}/{path}", data=payload, timeout=30)
        data = r.json()
        if "error" in data:
            msg = data["error"].get("message", str(data["error"]))
            raise InstagramPublishError.from_response(r, data, _friendly_error(msg))
        return data

    def create_image_container(
        self, ig_user_id: str, image_url: str, caption: str
    ) -> str:
        data = self._post(
            f"{ig_user_id}/media",
            image_url=image_url,
            caption=caption,
            media_type="IMAGE",
        )
        return data["id"]

    def create_video_container(
        self, ig_user_id: str, video_url: str, caption: str
    ) -> str:
        data = self._post(
            f"{ig_user_id}/media",
            video_url=video_url,
            caption=caption,
            media_type="REELS",
        )
        return data["id"]

    def create_reel_container(
        self, ig_user_id: str, video_url: str, caption: str
    ) -> str:
        return self.create_video_container(ig_user_id, video_url, caption)

    def create_story_image_container(self, ig_user_id: str, image_url: str) -> str:
        data = self._post(
            f"{ig_user_id}/media",
            image_url=image_url,
            media_type="STORIES",
        )
        return data["id"]

    def create_story_video_container(self, ig_user_id: str, video_url: str) -> str:
        data = self._post(
            f"{ig_user_id}/media",
            video_url=video_url,
            media_type="STORIES",
        )
        return data["id"]

    def create_carousel_container(
        self, ig_user_id: str, children: list[str], caption: str
    ) -> str:
        """children is a list of already-created container IDs (IMAGE type, is_carousel_item=True)."""
        data = self._post(
            f"{ig_user_id}/media",
            media_type="CAROUSEL",
            children=",".join(children),
            caption=caption,
        )
        return data["id"]

    def create_carousel_item_container(self, ig_user_id: str, image_url: str) -> str:
        data = self._post(
            f"{ig_user_id}/media",
            image_url=image_url,
            is_carousel_item=True,
        )
        return data["id"]

    def poll_container_status(self, container_id: str) -> str:
        """Returns one of: FINISHED, IN_PROGRESS, ERROR, EXPIRED, PUBLISHED."""
        data = self._get(container_id, fields="status_code")
        return data.get("status_code", "UNKNOWN")

    def get_permalink(self, media_id: str) -> str:
        data = self._get(media_id, fields="permalink")
        return data.get("permalink", "")

    def publish_container(self, ig_user_id: str, container_id: str) -> str:
        """Returns the published ig_media_id."""
        data = self._post(
            f"{ig_user_id}/media_publish",
            creation_id=container_id,
        )
        return data["id"]
