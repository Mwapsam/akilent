"""The install snippet for a chatbot's website widget.

The script file may be served from the static host (CloudFront in production) while the API is
always on SITE_URL, so the snippet names both: an absolute ``src`` that works when pasted on any
website, and ``data-api`` for where the widget sends its requests.
"""

from __future__ import annotations

from django.conf import settings
from django.templatetags.static import static

WIDGET_PATH = "chat-widget/v1/chat.js"


def api_base() -> str:
    return getattr(settings, "SITE_URL", "").rstrip("/")


def widget_src() -> str:
    url = static(WIDGET_PATH)
    if url.startswith("/"):  # local static files: make it absolute for other websites
        url = api_base() + url
    return url


def snippet(public_key: str) -> str:
    return (
        f'<script src="{widget_src()}"\n'
        f'        data-chatbot="{public_key}"\n'
        f'        data-api="{api_base()}"\n'
        "        async></script>"
    )
