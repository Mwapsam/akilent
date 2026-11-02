"""Facebook Pages publishing provider.

Wraps the Facebook Graph API for posting to a connected Page:
  - Text posts → POST /{page-id}/feed
  - Photo posts → POST /{page-id}/photos
  - Video posts → POST /{page-id}/videos
  - Reels → /{page-id}/video_reels (start, upload, finish)
  - Stories → /{page-id}/photo_stories and /{page-id}/video_stories
  - Multi-photo posts → unpublished /{page-id}/photos, then /{page-id}/feed

Requires scopes: pages_manage_posts, pages_read_engagement
Page access tokens are fetched with a long-lived user token, so they do not
expire (they stop working only if the user revokes access or changes their
password). They are stored encrypted.
"""

from __future__ import annotations

import json
import logging

import requests
from django.conf import settings

from .errors import MetaPublishError, parse_insights

logger = logging.getLogger(__name__)

_GRAPH_VERSION = settings.INSTAGRAM_GRAPH_VERSION
_BASE = f"https://graph.facebook.com/{_GRAPH_VERSION}"


class FacebookPublishError(MetaPublishError):
    pass


class _Stream:
    """A response body passed on as a request body without holding it in memory. The
    length lets ``requests`` send a Content-Length, which rupload needs."""

    def __init__(self, raw, length: int) -> None:
        self._raw = raw
        self._length = length

    def __len__(self) -> int:
        return self._length

    def read(self, size: int = -1) -> bytes:
        return self._raw.read(size)


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

    def create_text_post(self, message: str, link: str = "") -> str:
        """Returns the FB post id. ``link`` adds Facebook's link preview card."""
        payload = {"message": message}
        if link:
            payload["link"] = link
        data = self._post("feed", **payload)
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

    # --- Reels and stories (Graph v25 "Publish a Reel" and "Stories" guides) ---------
    # A video goes up in three steps: ``upload_phase=start`` on the edge returns a
    # video id, the file is sent to rupload.facebook.com (here by its hosted URL), and
    # ``upload_phase=finish`` publishes it. Meta then processes it; ``video_status``
    # reads where it is.

    def start_video_upload(self, edge: str) -> str:
        """``edge``: "video_reels" or "video_stories". Returns the video id."""
        data = self._post(edge, upload_phase="start")
        return str(data["video_id"])

    def upload_hosted_video(self, video_id: str, file_url: str) -> None:
        """Send Meta the file by URL. It fetches it as ``facebookexternalhit``."""
        self._rupload(video_id, {"file_url": file_url}, timeout=60)

    def upload_video_bytes(self, video_id: str, file_url: str, offset: int = 0) -> None:
        """Send Meta the file's bytes, streamed from ``file_url`` (our own media host;
        the caller checks), from ``offset`` on. The fallback when Meta can't fetch the
        URL itself, and how an interrupted upload resumes (``bytes_transfered``)."""
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with requests.get(
            file_url, headers=headers, stream=True, timeout=30, allow_redirects=False
        ) as src:
            src.raise_for_status()
            if offset and src.status_code != 206:
                raise RuntimeError("The media host ignored the byte range.")
            remaining = int(src.headers["Content-Length"])
            self._rupload(
                video_id,
                {
                    "offset": str(offset),
                    "file_size": str(offset + remaining),
                    "Content-Type": "application/octet-stream",
                },
                data=_Stream(src.raw, remaining),
                timeout=(10, 600),
            )

    def _rupload(self, video_id: str, headers: dict, *, data=None, timeout) -> None:
        r = requests.post(
            f"https://rupload.facebook.com/video-upload/{_GRAPH_VERSION}/{video_id}",
            headers={"Authorization": f"OAuth {self._token}", **headers},
            data=data,
            timeout=timeout,
        )
        body = r.json() if r.content else {}
        if "error" in body or r.status_code >= 400:
            raise FacebookPublishError.from_response(
                r, body if "error" in body else {"error": {"message": r.text[:200]}}
            )

    def finish_reel(self, video_id: str, description: str) -> None:
        self._post(
            "video_reels",
            video_id=video_id,
            upload_phase="finish",
            video_state="PUBLISHED",
            description=description,
        )

    def finish_video_story(self, video_id: str) -> str:
        """Returns the story's post id."""
        data = self._post("video_stories", video_id=video_id, upload_phase="finish")
        return str(data.get("post_id") or "")

    def video_status(self, video_id: str) -> dict:
        """The video's ``status`` object: video_status plus uploading_phase,
        processing_phase and publishing_phase, each with a status and maybe an error."""
        return self._get(video_id, fields="status").get("status") or {}

    def upload_unpublished_photo(self, image_url: str) -> str:
        """A photo held for a story or a multi-photo post. Meta deletes it after about
        24 hours if nothing publishes it. Returns the photo id."""
        data = self._post("photos", url=image_url, published="false")
        return str(data["id"])

    def create_photo_story(self, photo_id: str) -> str:
        """Returns the story's post id."""
        data = self._post("photo_stories", photo_id=photo_id)
        return str(data.get("post_id") or "")

    def create_multi_photo_post(self, photo_ids: list[str], message: str) -> str:
        """One feed post with every photo (``attached_media``). Returns the post id."""
        payload = {"message": message}
        for i, photo_id in enumerate(photo_ids):
            payload[f"attached_media[{i}]"] = json.dumps({"media_fbid": photo_id})
        data = self._post("feed", **payload)
        return data["id"]

    def create_comment(self, object_id: str, message: str) -> str:
        """Comment on a published post, photo or video as the Page
        (pages_manage_engagement). Returns the comment id."""
        payload = {"message": message, "access_token": self._token}
        r = requests.post(f"{_BASE}/{object_id}/comments", data=payload, timeout=30)
        data = r.json()
        if "error" in data:
            raise FacebookPublishError.from_response(r, data)
        return data["id"]

    def search_location_pages(self, query: str, limit: int = 8) -> list[dict]:
        """Pages for physical places matching ``query`` (Pages Search API), for an
        Instagram location tag. Only Pages with location data are returned."""
        data = self._get(
            "pages/search",
            q=query,
            fields="id,name,location",
            limit=limit * 2,
        )
        return [p for p in data.get("data", []) if p.get("location")][:limit]

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

    def fetch_page_insights(self, metrics: list[str], since: int, until: int) -> dict:
        """One day of the Page's insights (read_insights), ``period=day`` between two
        Unix times. A metric with no data is left out, never reported as 0."""
        return parse_insights(
            self._get(
                f"{self._page_id}/insights",
                metric=",".join(metrics),
                period="day",
                since=since,
                until=until,
            )
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


def fetch_user_id(user_access_token: str) -> str:
    """The app-scoped id of the person a user token belongs to (``/me``). Best effort."""
    try:
        r = requests.get(
            f"{_BASE}/me",
            params={"fields": "id", "access_token": user_access_token},
            timeout=30,
        )
        return str(r.json().get("id") or "")
    except Exception:
        logger.warning("Facebook /me lookup failed", exc_info=True)
        return ""


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
