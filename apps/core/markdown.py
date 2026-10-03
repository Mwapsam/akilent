"""Markdown content negotiation for public views.

When a client sends ``Accept: text/markdown`` (e.g. an AI agent), public views
call ``wants_markdown(request)`` and, when True, return a clean
``markdown_response()`` instead of the usual HTML page.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from django.http import HttpResponse
from django.utils.cache import patch_vary_headers

_MARKER_START = "<!-- MARKDOWN_START -->"
_MARKER_END = "<!-- MARKDOWN_END -->"

# Tags whose subtree is silently dropped (chrome, scripts, styles).
_SKIP_TAGS = frozenset(
    ["head", "style", "script", "nav", "footer", "header", "noscript"]
)

_HEADING_TAGS: dict[str, str] = {
    "h1": "#",
    "h2": "##",
    "h3": "###",
    "h4": "####",
    "h5": "#####",
    "h6": "######",
}

_BLOCK_TAGS = frozenset(["div", "section", "article", "main", "aside", "figure"])


def wants_markdown(request) -> bool:
    """Return True when the client explicitly accepts text/markdown."""
    return "text/markdown" in request.META.get("HTTP_ACCEPT", "")


def markdown_response(text: str, status: int = 200) -> HttpResponse:
    resp = HttpResponse(
        text.strip() + "\n",
        content_type="text/markdown; charset=utf-8",
        status=status,
    )
    patch_vary_headers(resp, ("Accept",))
    return resp


# ---------------------------------------------------------------------------
# HTML → Markdown
# ---------------------------------------------------------------------------


class _HtmlToMd(HTMLParser):
    """Minimal HTML → Markdown converter built on stdlib html.parser.

    Handles the tag set actually used in help articles, developer docs, and
    legal pages. Silently ignores chrome (nav, header, footer, style, script).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []
        self._skip: int = 0  # nesting depth inside _SKIP_TAGS
        self._pre: int = 0  # nesting depth inside <pre>
        self._code: int = 0  # nesting depth inside inline <code>
        self._strong: int = 0
        self._em: int = 0
        self._lists: list[str] = []  # stack of "ul" / "ol"
        self._counters: list[int] = []  # per-level ol counters
        self._href: str | None = None  # current <a> href
        self._in_th_row: bool = False
        self._cur_col: int = 0

    def _w(self, s: str) -> None:
        if not self._skip:
            self._buf.append(s)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return

        if tag == "pre":
            self._pre += 1
            lang = a.get("data-lang", "")
            self._w(f"\n\n```{lang}\n")
        elif tag == "code" and not self._pre:
            self._code += 1
            self._w("`")
        elif tag in _HEADING_TAGS:
            self._w(f"\n\n{_HEADING_TAGS[tag]} ")
        elif tag == "p" or tag in _BLOCK_TAGS:
            self._w("\n\n")
        elif tag == "br":
            self._w("  \n")
        elif tag == "hr":
            self._w("\n\n---\n\n")
        elif tag in ("ul", "ol"):
            self._lists.append(tag)
            if tag == "ol":
                self._counters.append(0)
        elif tag == "li":
            depth = max(0, len(self._lists) - 1)
            indent = "  " * depth
            if self._lists and self._lists[-1] == "ol":
                self._counters[-1] += 1
                self._w(f"\n{indent}{self._counters[-1]}. ")
            else:
                self._w(f"\n{indent}- ")
        elif tag == "a":
            self._href = a.get("href", "") or ""
            if self._href:
                self._w("[")
        elif tag in ("strong", "b"):
            self._strong += 1
            self._w("**")
        elif tag in ("em", "i"):
            self._em += 1
            self._w("*")
        elif tag == "blockquote":
            self._w("\n\n> ")
        elif tag == "table":
            self._w("\n\n")
        elif tag == "tr":
            self._in_th_row = False
            self._cur_col = 0
        elif tag == "th":
            self._in_th_row = True
            self._cur_col += 1
            self._w("| ")
        elif tag == "td":
            self._cur_col += 1
            self._w("| ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return

        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._w("\n```\n\n")
        elif tag == "code" and not self._pre:
            self._code = max(0, self._code - 1)
            self._w("`")
        elif tag in _HEADING_TAGS or tag == "p" or tag in _BLOCK_TAGS:
            self._w("\n\n")
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            if tag == "ol" and self._counters:
                self._counters.pop()
            if not self._lists:
                self._w("\n")
        elif tag == "a":
            if self._href:
                self._w(f"]({self._href})")
            self._href = None
        elif tag in ("strong", "b"):
            self._strong = max(0, self._strong - 1)
            self._w("**")
        elif tag in ("em", "i"):
            self._em = max(0, self._em - 1)
            self._w("*")
        elif tag == "blockquote":
            self._w("\n\n")
        elif tag in ("th", "td"):
            self._w(" ")
        elif tag == "tr":
            self._w("|\n")
            if self._in_th_row and self._cur_col:
                self._w("|" + "|".join([" --- "] * self._cur_col) + "|\n")
        elif tag == "table":
            self._w("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._pre:
            self._w(data)
            return
        # Collapse whitespace outside pre blocks.
        normalized = " ".join(data.split())
        if normalized:
            self._w(normalized)
            if data and data[-1] in (" ", "\t"):
                self._w(" ")

    def get_markdown(self) -> str:
        text = "".join(self._buf)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html: str) -> str:
    parser = _HtmlToMd()
    parser.feed(html)
    return parser.get_markdown()


def extract_and_convert(html: str) -> str:
    """Extract content between MARKDOWN_START/END markers and convert to Markdown.

    Falls back to converting the whole document if markers are absent.
    """
    s = html.find(_MARKER_START)
    e = html.find(_MARKER_END)
    if s != -1 and e != -1:
        return html_to_markdown(html[s + len(_MARKER_START) : e])
    return html_to_markdown(html)
