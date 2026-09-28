"""In-place navigation for the signed-in app ("shell swaps").

The app shell (sidebar, topbar, command palette, toasts) stays loaded; links and forms are boosted
by HTMX, and ``base.html`` answers a boosted request with just the page: its ``<title>``, what
goes in ``<main id="main">``, and out-of-band updates for the parts of the shell that depend on
the page (nav active state, breadcrumbs). Views don't change.

``ShellMiddleware`` keeps that safe. Whenever a fragment can't be used, it tells HTMX to do an
ordinary full page load instead (``HX-Redirect``), so the worst case is today's behaviour:

- the page belongs to a different shell (operator console, signed-out pages),
- the response is a whole document (landing page, docs, anything not built on ``base.html``),
- the page ships its own scripts that expect a fresh page load (see ``_needs_full_load``),
- the response is a file download, or a redirect to another site (checkout, Meta sign-up),
- the view is marked ``@full_page_load``.

Background requests (``hx-get``/``hx-post`` without boosting) are untouched: use
``is_background`` for "should I return a partial?", never a bare ``HX-Request`` check, because
boosted navigations send ``HX-Request`` too.
"""

from __future__ import annotations

import re
from functools import wraps
from urllib.parse import urlsplit

SHELL_HEADER = "X-Akilent-Shell"
_MARKER = re.compile(rb'<meta name="akilent-shell" content="([a-z-]+)"')
# A page script is fragment-safe only when it says so. Everything else (DOMContentLoaded
# initialisers, global listeners) would silently not run, or run twice, after a swap.
_SCRIPT_TAG = re.compile(rb"<script\b[^>]*>", re.IGNORECASE)
_SAFE_SCRIPT = re.compile(rb'type="application/json"|data-shell-safe', re.IGNORECASE)


def is_boosted(request) -> bool:
    return request.headers.get("HX-Boosted") == "true"


def is_background(request) -> bool:
    """An HTMX request for a piece of a page, as opposed to a boosted navigation."""
    return request.headers.get("HX-Request") == "true" and not is_boosted(request)


def is_shell_swap(request) -> bool:
    """Should ``base.html`` render only the page, for swapping into the current shell?"""
    return (
        is_boosted(request)
        and request.headers.get("HX-History-Restore-Request") != "true"
        and bool(request.headers.get(SHELL_HEADER))
    )


def full_page_load(view):
    """Always answer boosted navigations to this view with a real page load.

    For pages whose scripts can't yet cope with being swapped in (rich editors).
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if is_shell_swap(request):
            return _hx_redirect(request.get_full_path())
        return view(request, *args, **kwargs)

    wrapper.full_page_load = True
    return wrapper


def _hx_redirect(url: str):
    from django.http import HttpResponse

    response = HttpResponse(status=204)
    response["HX-Redirect"] = url
    return response


def _needs_full_load(content: bytes) -> bool:
    return any(not _SAFE_SCRIPT.search(tag) for tag in _SCRIPT_TAG.findall(content))


class ShellMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.shell_swap = is_shell_swap(request)
        response = self.get_response(request)
        _vary(response)
        if not is_boosted(request):
            return response
        return self._boosted(request, response)

    def _boosted(self, request, response):
        # Redirects: HTMX follows same-origin ones itself; another host needs the browser.
        if 300 <= response.status_code < 400 and response.has_header("Location"):
            target = urlsplit(response["Location"])
            if target.netloc and target.netloc != request.get_host():
                return _hx_redirect(response["Location"])
            return response

        if response.has_header("HX-Redirect") or response.has_header("HX-Refresh"):
            return response
        if "attachment" in response.get("Content-Disposition", ""):
            return self._reload(request)
        if response.streaming or "text/html" not in response.get("Content-Type", ""):
            return response

        content = response.content
        marker = _MARKER.search(content[:2048])
        if marker is None or content.lstrip()[:15].lower().startswith(b"<!doctype"):
            return self._reload(
                request
            )  # a whole document, not a fragment of this shell
        if marker.group(1).decode() != request.headers.get(SHELL_HEADER):
            return self._reload(request)
        if _needs_full_load(content):
            return self._reload(request)

        if request.method == "GET" and response.status_code == 200:
            response["HX-Push-Url"] = request.get_full_path()
            # htmx-ext-preload (base.html) warms a sidebar link's target on hover, then the
            # real navigation re-requests the same URL moments later. Without a Cache-Control
            # that allows reuse, the browser refetches from the network both times and the
            # hover request was pure waste. `private` keeps it out of any shared/CDN cache;
            # 5s is long enough to cover hover-then-click but short enough that a page cached
            # just before a sign-out or permission change is stale for only a few seconds.
            # A view that has its own opinion (e.g. an explicit no-store) keeps it.
            if not response.has_header("Cache-Control"):
                response["Cache-Control"] = "private, max-age=5"
        return response

    @staticmethod
    def _reload(request):
        """Load this page for real. A POST that already ran is not repeated: refresh instead."""
        if request.method == "GET":
            return _hx_redirect(request.get_full_path())
        from django.http import HttpResponse

        response = HttpResponse(status=204)
        response["HX-Refresh"] = "true"
        return response


def _vary(response):
    from django.utils.cache import patch_vary_headers

    # Cookie matters here beyond the usual "don't cache someone else's session" reason: a boosted
    # GET's Cache-Control (below) makes the browser's own HTTP cache — not just Django's — willing
    # to reuse the response. Without Vary: Cookie, that cache is keyed on URL alone, so on a shared
    # browser (a kiosk, a shared machine) user B's click could be served user A's cached fragment
    # from before A signed out, entirely client-side, with nothing hitting the server to notice.
    patch_vary_headers(response, ("HX-Request", "HX-Boosted", "Cookie"))
