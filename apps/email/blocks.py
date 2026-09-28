"""The block editor's email format: a list of sections that Akilent turns into email-safe HTML.

The editor (``static/js/email-blocks.js``) only ever sends this document, never HTML. ``clean``
checks it (types, lengths, colours, links) and ``render`` builds the HTML and plain-text bodies
here, on the server, with every piece of text escaped, so what's saved is always a layout that
works in email clients: tables, inline styles, a 600px column that narrows on phones, and two
columns that stack on a small screen without relying on media queries.

Text is plain words plus ``{{ blank }}`` merge tags (``**bold**`` is the only formatting). Template
code (``{% %}``) is removed from text because email bodies go through the template engine. The one
exception is the "html" section, which holds hand-written HTML exactly like the raw editor does;
it's how a design from the old drag-and-drop editor, or raw HTML, is carried into this one.

Stored in ``EmailTemplate.content_blocks`` (with ``builder_mode="blocks"``). A document is
recognised by ``{"format": FORMAT}``; anything else there (the old editor's project data, or what
an API client stored) is left alone and opened as one "html" section.
"""

from __future__ import annotations

import re
import uuid
from html import escape

FORMAT = "akilent-blocks"
VERSION = 1

MAX_BLOCKS = 60
TYPES = (
    "heading",
    "text",
    "image",
    "button",
    "divider",
    "spacer",
    "columns",
    "footer",
    "html",
)
FONTS = {
    "sans": "Arial,Helvetica,sans-serif",
    "serif": "Georgia,'Times New Roman',serif",
    "rounded": "'Trebuchet MS',Verdana,sans-serif",
    "mono": "'Courier New',Courier,monospace",
}
ALIGNS = ("left", "center", "right")
HEADING_SIZES = {"small": 18, "medium": 22, "large": 28}
CAPS = {
    "heading": 200,
    "text": 5000,
    "label": 60,
    "alt": 200,
    "url": 2000,
    "footer": 1000,
    "html": 200_000,
    "preheader": 200,
    "column_text": 2000,
}
DEFAULT_STYLE = {
    "background": "#F3F5F7",
    "content_background": "#FFFFFF",
    "text_color": "#12182B",
    "accent": "#FFB020",
    "button_text": "#12182B",
    "font": "sans",
    "rounded": True,
}
WIDTH = 600
PADDING = 32

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_TAG = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}\}")
_CODE = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.S)
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_URL = re.compile(r"^(https?://[^\s\"'<>]+|mailto:[^\s\"'<>]+|tel:[+0-9 ()-]+)$", re.I)
_ONLY_TAG = re.compile(r"^\{\{\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*\s*\}\}$")


class BlocksError(ValueError):
    """The document can't be saved; the message is shown to the owner as is."""


# ---- recognising and starting documents -----------------------------------------------------


def is_document(data) -> bool:
    return isinstance(data, dict) and data.get("format") == FORMAT


def new_id() -> str:
    return "b" + uuid.uuid4().hex[:10]


def starter(company_name: str = "") -> dict:
    """A new email: something to change rather than a blank page."""
    name = (company_name or "").strip() or "Your business"
    return {
        "format": FORMAT,
        "version": VERSION,
        "style": dict(DEFAULT_STYLE),
        "preheader": "",
        "blocks": [
            {
                "id": new_id(),
                "type": "heading",
                "text": "Hi {{ first_name }},",
                "size": "large",
                "align": "left",
            },
            {
                "id": new_id(),
                "type": "text",
                "align": "left",
                "text": "Write your message here. Keep it short and friendly.\n\nAdd a button below if you'd "
                "like people to do something next.",
            },
            {
                "id": new_id(),
                "type": "button",
                "label": "Find out more",
                "href": "https://example.com",
                "align": "left",
                "color": "",
                "text_color": "",
            },
            {
                "id": new_id(),
                "type": "footer",
                "align": "center",
                "text": f"{name}\nYou're receiving this because you're a customer of {name}.",
            },
        ],
    }


def from_html(html: str, *, reason: str = "") -> dict:
    """Carry existing HTML into the editor as one "html" section, so nothing is lost or changed."""
    doc = {
        "format": FORMAT,
        "version": VERSION,
        "style": dict(DEFAULT_STYLE),
        "preheader": "",
        "blocks": [{"id": new_id(), "type": "html", "html": html or ""}],
    }
    if reason:
        doc["imported"] = reason
    return doc


def document_for(template, *, base_url: str = "") -> dict:
    """What the editor should open for ``template``.

    A saved block document opens as is only while it still produces the template's HTML. If the
    HTML was changed another way since (the HTML editor, or the API, which can update ``html``
    without touching ``builder_mode``), the current HTML is what's real: it opens as one "html"
    section, so saving never puts the older design back over it. ``base_url`` must be the one
    the save used, since it's part of the HTML (uploaded images). A template with no document
    starts from its HTML, or from ``starter``.
    """
    data = template.content_blocks
    if is_document(data):
        try:
            doc = clean(data)
        except BlocksError:
            doc = None
        if doc is not None:
            if not (template.html_body or "").strip():
                return doc
            html, _ = render(doc, base_url=base_url)
            if _same(html, template.html_body):
                return doc
        return from_html(template.html_body, reason="raw")
    if (template.html_body or "").strip():
        reason = "classic" if isinstance(data, dict) and data.get("pages") else "raw"
        return from_html(template.html_body, reason=reason)
    return starter(getattr(template.account, "company_name", ""))


def _same(a: str, b: str) -> bool:
    return re.sub(r"\s+", "", a or "") == re.sub(r"\s+", "", b or "")


# ---- checking --------------------------------------------------------------------------------


def _text(value, cap: int) -> str:
    text = _CODE.sub("", str(value or ""))
    text = text.replace("{%", "").replace("%}", "").replace("{#", "").replace("#}", "")
    return text.replace("\r\n", "\n").replace("\r", "\n")[:cap]


def _colour(value, fallback: str) -> str:
    value = str(value or "").strip()
    return value.upper() if _HEX.match(value) else fallback


def _choice(value, options, fallback):
    return value if value in options else fallback


def _url(value, *, label: str, required: bool = False) -> str:
    """A link: https://, mailto:, tel:, a site path (for uploaded images), or exactly one blank."""
    value = _CODE.sub("", str(value or "")).strip()[: CAPS["url"]]
    if not value:
        if required:
            raise BlocksError(f"{label} needs a link.")
        return ""
    if _ONLY_TAG.match(value):
        return "{{ " + _TAG.match(value).group(1) + " }}"
    if value.startswith("/") and not value.startswith("//"):
        return value
    if value.lower().startswith("www."):
        value = "https://" + value
    if not _URL.match(value):
        raise BlocksError(
            f'{label}: "{value[:60]}" isn\'t a link. Use one starting with https://, '
            "or a blank like {{ link }}."
        )
    return value


def _block_id(value) -> str:
    value = str(value or "")
    return value if _ID.match(value) else new_id()


def _column(data) -> dict:
    data = data if isinstance(data, dict) else {}
    image = data.get("image") if isinstance(data.get("image"), dict) else {}
    button = data.get("button") if isinstance(data.get("button"), dict) else {}
    src = _url(image.get("src"), label="A column's image")
    label = _text(button.get("label"), CAPS["label"]).strip()
    return {
        "image": {"src": src, "alt": _text(image.get("alt"), CAPS["alt"]).strip()},
        "heading": _text(data.get("heading"), CAPS["heading"]).strip(),
        "text": _text(data.get("text"), CAPS["column_text"]).strip(),
        "button": {
            "label": label,
            "href": _url(
                button.get("href"),
                label=f'The "{label or "column"}" button',
                required=bool(label),
            ),
        },
    }


def _block(data: dict) -> dict:
    kind = data.get("type")
    block = {"id": _block_id(data.get("id")), "type": kind}
    align = _choice(data.get("align"), ALIGNS, "left")
    if kind == "heading":
        block.update(
            text=_text(data.get("text"), CAPS["heading"]).strip(),
            align=align,
            size=_choice(data.get("size"), HEADING_SIZES, "medium"),
        )
    elif kind == "text":
        block.update(text=_text(data.get("text"), CAPS["text"]).strip(), align=align)
    elif kind == "image":
        try:
            width = int(data.get("width") or 100)
        except (TypeError, ValueError):
            width = 100
        block.update(
            src=_url(data.get("src"), label="The image"),
            alt=_text(data.get("alt"), CAPS["alt"]).strip(),
            href=_url(data.get("href"), label="The image's link"),
            width=min(100, max(20, width)),
            align=_choice(data.get("align"), ALIGNS, "center"),
        )
    elif kind == "button":
        label = _text(data.get("label"), CAPS["label"]).strip()
        if not label:
            raise BlocksError("A button needs a label.")
        block.update(
            label=label,
            href=_url(data.get("href"), label=f'The "{label}" button', required=True),
            align=align,
            color=_colour(data.get("color"), ""),
            text_color=_colour(data.get("text_color"), ""),
            full_width=bool(data.get("full_width")),
        )
    elif kind == "divider":
        block.update(color=_colour(data.get("color"), ""))
    elif kind == "spacer":
        try:
            height = int(data.get("height") or 24)
        except (TypeError, ValueError):
            height = 24
        block.update(height=min(120, max(8, height)))
    elif kind == "columns":
        columns = data.get("columns") if isinstance(data.get("columns"), list) else []
        block.update(columns=[_column(c) for c in (columns + [{}, {}])[:2]])
    elif kind == "footer":
        block.update(
            text=_text(data.get("text"), CAPS["footer"]).strip(),
            align=_choice(data.get("align"), ALIGNS, "center"),
        )
    elif kind == "html":
        block.update(html=str(data.get("html") or "")[: CAPS["html"]])
    else:
        raise BlocksError("That section type isn't supported.")
    return block


def clean(data) -> dict:
    """A checked document, or ``BlocksError``. Safe to call on anything the browser sent."""
    if not is_document(data):
        raise BlocksError("The email couldn't be read. Reload the page and try again.")
    raw_blocks = data.get("blocks")
    if not isinstance(raw_blocks, list):
        raise BlocksError("The email couldn't be read. Reload the page and try again.")
    if len(raw_blocks) > MAX_BLOCKS:
        raise BlocksError(f"An email can have up to {MAX_BLOCKS} sections.")
    style_in = data.get("style") if isinstance(data.get("style"), dict) else {}
    style = {
        "background": _colour(style_in.get("background"), DEFAULT_STYLE["background"]),
        "content_background": _colour(
            style_in.get("content_background"), DEFAULT_STYLE["content_background"]
        ),
        "text_color": _colour(style_in.get("text_color"), DEFAULT_STYLE["text_color"]),
        "accent": _colour(style_in.get("accent"), DEFAULT_STYLE["accent"]),
        "button_text": _colour(
            style_in.get("button_text"), DEFAULT_STYLE["button_text"]
        ),
        "font": _choice(style_in.get("font"), FONTS, "sans"),
        "rounded": bool(style_in.get("rounded", True)),
    }
    blocks, seen = [], set()
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            continue
        block = _block(raw)
        if block["id"] in seen:
            block["id"] = new_id()
        seen.add(block["id"])
        blocks.append(block)
    doc = {
        "format": FORMAT,
        "version": VERSION,
        "style": style,
        "preheader": _text(data.get("preheader"), CAPS["preheader"]).strip(),
        "blocks": blocks,
    }
    if data.get("imported") in ("classic", "raw"):
        doc["imported"] = data["imported"]
    return doc


def blanks_in(doc: dict) -> list[str]:
    """Every ``{{ blank }}`` the email uses, in order of first use (for sample data)."""
    found: list[str] = []

    def scan(value):
        if isinstance(value, str):
            for name in _TAG.findall(value):
                if name not in found:
                    found.append(name)
        elif isinstance(value, dict):
            for v in value.values():
                scan(v)
        elif isinstance(value, list):
            for v in value:
                scan(v)

    scan(doc.get("preheader"))
    scan([b for b in doc.get("blocks", []) if b.get("type") != "html"])
    return found


# ---- rendering -------------------------------------------------------------------------------


def _absolute(url: str, base_url: str) -> str:
    if url.startswith("/") and base_url:
        return base_url.rstrip("/") + url
    return url


def _inline(text: str) -> str:
    """Escaped text; line breaks become <br>, **bold** becomes <strong>. Blanks pass through."""
    out = escape(text)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return out.replace("\n", "<br>")


def _paragraphs(text: str) -> list[str]:
    return [p.strip("\n") for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def _plain(text: str) -> str:
    return _BOLD.sub(r"\1", text or "")


def _button_html(
    label: str,
    href: str,
    style: dict,
    *,
    color: str = "",
    text_color: str = "",
    full_width: bool = False,
    align: str = "left",
) -> str:
    bg = color or style["accent"]
    fg = text_color or style["button_text"]
    radius = "8px" if style["rounded"] else "0"
    width = "width:100%;" if full_width else ""
    link = (
        f'<a href="{escape(href, quote=True)}" target="_blank" '
        f'style="display:inline-block;{width}box-sizing:border-box;background:{bg};color:{fg};'
        f"font-weight:600;font-size:15px;line-height:1.2;text-decoration:none;padding:12px 24px;"
        f'border-radius:{radius};text-align:center;">{escape(label)}</a>'
    )
    return f'<div style="text-align:{align};margin:0 0 20px;">{link}</div>'


def _image_html(
    src: str,
    alt: str,
    href: str,
    width_pct: int,
    align: str,
    max_px: int,
    rounded: bool,
    base_url: str,
) -> str:
    if not src:
        return ""
    px = round(max_px * width_pct / 100)
    radius = "8px" if rounded else "0"
    img = (
        f'<img src="{escape(_absolute(src, base_url), quote=True)}" alt="{escape(alt, quote=True)}" '
        f'width="{px}" style="display:inline-block;width:100%;max-width:{px}px;height:auto;border:0;'
        f'border-radius:{radius};">'
    )
    if href:
        img = f'<a href="{escape(href, quote=True)}" target="_blank">{img}</a>'
    return f'<div style="text-align:{align};margin:0 0 20px;line-height:0;">{img}</div>'


def _column_html(col: dict, style: dict, base_url: str, width: int) -> str:
    parts = []
    image = col.get("image") or {}
    if image.get("src"):
        parts.append(
            _image_html(
                image["src"],
                image.get("alt", ""),
                "",
                100,
                "center",
                width,
                style["rounded"],
                base_url,
            )
        )
    if col.get("heading"):
        parts.append(
            f'<h3 style="margin:0 0 8px;font-size:17px;line-height:1.3;font-weight:700;">'
            f"{_inline(col['heading'])}</h3>"
        )
    for p in _paragraphs(col.get("text", "")):
        parts.append(
            f'<p style="margin:0 0 12px;font-size:14px;line-height:1.6;">{_inline(p)}</p>'
        )
    button = col.get("button") or {}
    if button.get("label") and button.get("href"):
        parts.append(_button_html(button["label"], button["href"], style))
    return "".join(parts)


def render(data, *, base_url: str = "") -> tuple[str, str]:
    """``(html_body, text_body)`` for a document (checked again here). ``base_url`` makes /media/
    images absolute, since an email can't load a path from Akilent's own site."""
    doc = clean(data)
    style = doc["style"]
    font = FONTS[style["font"]]
    inner = WIDTH - 2 * PADDING
    radius = "12px" if style["rounded"] else "0"
    html: list[str] = []
    text: list[str] = []

    for b in doc["blocks"]:
        kind = b["type"]
        if kind == "heading" and b["text"]:
            size = HEADING_SIZES[b["size"]]
            html.append(
                f'<h2 style="margin:0 0 16px;font-size:{size}px;line-height:1.25;font-weight:700;'
                f'text-align:{b["align"]};">{_inline(b["text"])}</h2>'
            )
            text.append(_plain(b["text"]))
        elif kind == "text" and b["text"]:
            for p in _paragraphs(b["text"]):
                html.append(
                    f'<p style="margin:0 0 16px;font-size:15px;line-height:1.6;text-align:{b["align"]};">'
                    f"{_inline(p)}</p>"
                )
                text.append(_plain(p))
        elif kind == "image" and b["src"]:
            html.append(
                _image_html(
                    b["src"],
                    b["alt"],
                    b["href"],
                    b["width"],
                    b["align"],
                    inner,
                    style["rounded"],
                    base_url,
                )
            )
            if b["alt"]:
                text.append(f"[{b['alt']}]" + (f" {b['href']}" if b["href"] else ""))
        elif kind == "button":
            html.append(
                _button_html(
                    b["label"],
                    b["href"],
                    style,
                    color=b["color"],
                    text_color=b["text_color"],
                    full_width=b["full_width"],
                    align=b["align"],
                )
            )
            text.append(f"{b['label']}: {b['href']}")
        elif kind == "divider":
            color = b["color"] or "#E8EDF3"
            html.append(
                f'<hr style="border:0;border-top:1px solid {color};margin:8px 0 24px;">'
            )
            text.append("----")
        elif kind == "spacer":
            html.append(
                f'<div style="height:{b["height"]}px;line-height:{b["height"]}px;font-size:1px;">&nbsp;</div>'
            )
        elif kind == "columns":
            col_px = (inner - 24) // 2
            cells = []
            for col in b["columns"]:
                cells.append(
                    f'<div class="ak-col" style="display:inline-block;width:100%;max-width:{col_px}px;'
                    f'vertical-align:top;text-align:left;font-size:14px;margin:0 6px;">'
                    f"{_column_html(col, style, base_url, col_px)}</div>"
                )
                for piece in (col.get("heading"), col.get("text")):
                    if piece:
                        text.append(_plain(piece))
                button = col.get("button") or {}
                if button.get("label") and button.get("href"):
                    text.append(f"{button['label']}: {button['href']}")
            html.append(
                f'<div style="font-size:0;text-align:center;margin:0 -6px 8px;">{"".join(cells)}</div>'
            )
        elif kind == "footer" and b["text"]:
            html.append(
                f'<div style="margin:24px 0 0;padding-top:16px;border-top:1px solid #E8EDF3;font-size:12px;'
                f'line-height:1.6;color:#657089;text-align:{b["align"]};">'
                + "".join(
                    f'<p style="margin:0 0 8px;">{_inline(p)}</p>'
                    for p in _paragraphs(b["text"])
                )
                + "</div>"
            )
            text.append(_plain(b["text"]))
        elif kind == "html" and b["html"].strip():
            html.append(b["html"])

    preheader = ""
    if doc["preheader"]:
        preheader = (
            f'<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;'
            f'mso-hide:all;">{escape(doc["preheader"])}</div>'
        )
    body = (
        f"{preheader}"
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background:{style["background"]};">'
        f'<tr><td align="center" style="padding:24px 12px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="max-width:{WIDTH}px;background:{style["content_background"]};border-radius:{radius};">'
        f'<tr><td style="padding:{PADDING}px 24px;font-family:{font};color:{style["text_color"]};">'
        + "\n".join(html)
        + "</td></tr></table></td></tr></table>"
    )
    return body, "\n\n".join(t for t in text if t.strip())


def sample_values(doc: dict, current: dict | None, company_name: str = "") -> dict:
    """Sample data with a value for every blank the email uses, keeping what the owner already set."""
    values = dict(current or {})
    values.setdefault("company_name", company_name or "Your business")
    for name in blanks_in(doc):
        if "." in name or name in values:
            continue
        values[name] = (
            "https://example.com"
            if name.endswith(("url", "link"))
            else name.replace("_", " ").title()
        )
    return values
