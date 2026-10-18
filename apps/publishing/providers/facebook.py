"""Facebook Pages publishing provider.

Wraps the Facebook Graph API for posting to a connected Page:
  - Text posts → POST /{page-id}/feed
  - Photo posts → POST /{page-id}/photos
  - Video posts → POST /{page-id}/videos

Requires scopes: pages_manage_posts, pages_read_engagement
Page access tokens are fetched with a long-lived user token, so they do not
expire (they stop working only if the user revokes access or changes their
password). They are stored encrypted.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

from .errors import MetaPublishError, parse_insights

logger = logging.getLogger(__name__)

_GRAPH_VERSION = settings.INSTAGRAM_GRAPH_VERSION
_BASE = f"https://graph.facebook.com/{_GRAPH_VERSION}"


class FacebookPublishError(MetaPublishError):
    pass


class FacebookPublishProvider:
    def __init__(self, page_id: str, page_access_token: str) -> None:
        self._page_id = page_id
        self._token = page_access_token

    def _post(self, endpoint: str, **payload) -> dict:
        payload["access_token"] = self._token
        r = requests.post(
            f"{_BASE}/{self._page_id}/{endpoint}", data=payload, timeout=30
        )
        data = r.json()
        if "error" in data:
            raise FacebookPublishError.from_response(r, data)
        return data

    def create_text_post(self, message: str) -> str:
        """Returns the FB post id."""
        data = self._post("feed", message=message)
        return data["id"]

    def create_photo_post(self, image_url: str, caption: str) -> str:
        """Returns the FB post id."""
        data = self._post("photos", url=image_url, caption=caption)
        return data["id"]

    def create_video_post(
        self, video_url: str, description: str, title: str = ""
    ) -> str:
        """Returns the FB video id."""
        payload: dict = {"file_url": video_url, "description": description}
        if title:
            payload["title"] = title
        data = self._post("videos", **payload)
        return data["id"]

    def _get(self, path: str, **params) -> dict:
        params["access_token"] = self._token
        r = requests.get(f"{_BASE}/{path}", params=params, timeout=30)
        data = r.json()
        if "error" in data:
            raise FacebookPublishError.from_response(r, data)
        return data

    def fetch_fields(self, object_id: str, fields: list[str]) -> dict:
        return self._get(object_id, fields=",".join(fields))

    def resolve_post_id(self, object_id: str) -> str:
        """The Page post behind a published object, for post insights.

        A text post's id already is the post id (``{page-id}_{post-id}``). A photo names
        its post in ``page_story_id`` and a video in ``post_id``.
        """
        if "_" in object_id:
            return object_id
        for field in ("page_story_id", "post_id"):
            try:
                data = self._get(object_id, fields=field)
            except FacebookPublishError as exc:
                if exc.code == 100:  # not a field of this object type
                    continue
                raise
            if data.get(field):
                return data[field]
        return ""

    def fetch_post_insights(self, post_id: str, metrics: list[str]) -> dict:
        """Lifetime insights for a Page post (read_insights + pages_read_engagement).

        Returns {metric: value} for the metrics Facebook reported; one with no data is
        left out, never reported as 0.
        """
        return parse_insights(
            self._get(f"{post_id}/insights", metric=",".join(metrics))
        )


def exchange_for_long_lived_user_token(
    app_id: str, app_secret: str, short_lived_token: str
) -> str:
    """Swap a short-lived user token (~1 hour) for a long-lived one (~60 days).

    Page tokens fetched with a long-lived user token do not expire; page tokens
    fetched with a short-lived one expire with it.
    """
    r = requests.get(
        f"{_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_lived_token,
        },
        timeout=30,
    )
    data = r.json()
    if "error" in data:
        raise FacebookPublishError.from_response(r, data)
    return data["access_token"]


def get_page_token(user_access_token: str, page_id: str) -> str:
    """Exchange a short-lived user token for a long-lived page token."""
    r = requests.get(
        f"{_BASE}/{page_id}",
        params={"fields": "access_token", "access_token": user_access_token},
        timeout=30,
    )
    data = r.json()
    if "error" in data:
        raise FacebookPublishError.from_response(r, data)
    return data["access_token"]


def list_pages(user_access_token: str) -> list[dict]:
    """Return [{id, name, access_token}] for pages the user manages.

    Tries /me/accounts first (direct Page roles). Falls back to
    /{business-id}/owned_pages for Business Manager-managed pages.
    """
    r = requests.get(
        f"{_BASE}/me/accounts",
        params={"access_token": user_access_token, "fields": "id,name,access_token"},
        timeout=30,
    )
    data = r.json()
    if "error" in data:
        raise FacebookPublishError.from_response(r, data)
    pages = data.get("data", [])
    if pages:
        return pages

    # No direct Page roles — try Business Manager owned pages.
    biz_r = requests.get(
        f"{_BASE}/me/businesses",
        params={"access_token": user_access_token, "fields": "id,name"},
        timeout=30,
    )
    biz_data = biz_r.json()
    if "error" in biz_data:
        return []

    for biz in biz_data.get("data", []):
        biz_id = biz.get("id")
        if not biz_id:
            continue
        p_r = requests.get(
            f"{_BASE}/{biz_id}/owned_pages",
            params={
                "access_token": user_access_token,
                "fields": "id,name,access_token",
            },
            timeout=30,
        )
        p_data = p_r.json()
        if "error" not in p_data:
            pages.extend(p_data.get("data", []))

    return pages
