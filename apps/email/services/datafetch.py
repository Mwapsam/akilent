"""Render-time dynamic-data fetch for templates.

A template can declare named data sources (``EmailTemplateDataSource``); each is
fetched over HTTPS at render and injected into the variable context under its
key (e.g. ``{{ order.total }}`` where ``order`` is the source key). Fetches are:

* HTTPS only, and blocked if the host resolves to a private / loopback /
  link-local / reserved address (SSRF guard),
* size-capped and time-limited,
* cached per URL for the source's TTL,
* best-effort — any failure yields ``{}`` for that key and never breaks a send.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
from urllib.parse import urlsplit

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5
_MAX_BYTES = 256 * 1024


class DataFetchError(Exception):
    pass


def _assert_public_https(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise DataFetchError("data source URL must be https")
    host = parts.hostname
    if not host:
        raise DataFetchError("data source URL has no host")
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise DataFetchError(f"cannot resolve {host}") from exc
    for family, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        ):
            raise DataFetchError(f"{host} resolves to a non-public address")
    return host


def fetch_json(url: str, *, headers: dict | None = None, ttl_seconds: int = 300) -> dict:
    """Fetch and JSON-decode ``url``. Cached by URL for ``ttl_seconds``.

    Returns a dict (a non-object JSON body is wrapped as ``{"value": <body>}``).
    Raises :class:`DataFetchError` on any problem.
    """
    cache_key = f"tmpl_data:{url}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    _assert_public_https(url)
    try:
        resp = requests.get(
            url,
            headers={"Accept": "application/json", **(headers or {})},
            timeout=_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise DataFetchError(str(exc)) from exc

    if resp.status_code >= 400:
        raise DataFetchError(f"HTTP {resp.status_code}")
    body = resp.raw.read(_MAX_BYTES + 1, decode_content=True)
    if len(body) > _MAX_BYTES:
        raise DataFetchError(f"response exceeds {_MAX_BYTES} bytes")
    try:
        parsed = json.loads(body or b"{}")
    except ValueError as exc:
        raise DataFetchError("response is not valid JSON") from exc

    result = parsed if isinstance(parsed, dict) else {"value": parsed}
    cache.set(cache_key, result, max(int(ttl_seconds), 0) or 1)
    return result


def resolve_data_sources(template) -> dict:
    """Return ``{key: fetched_dict}`` for every data source on ``template``.

    A source that fails to fetch contributes ``{key: {}}`` so template authors
    can guard with ``{% if %}`` and a send never fails on an upstream outage.
    """
    get_sources = getattr(template, "data_sources", None)
    if get_sources is None:
        return {}
    out: dict = {}
    for src in get_sources.all():
        try:
            out[src.key] = fetch_json(
                src.url, headers=src.headers or None, ttl_seconds=src.ttl_seconds
            )
        except DataFetchError as exc:
            logger.warning("template %s data source %s failed: %s", template.pk, src.key, exc)
            out[src.key] = {}
    return out
