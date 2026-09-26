"""Turn AI-written plain-text parts into an email template, in Akilent's own layout.

The model never writes HTML or template code: email bodies are rendered through a template
engine (``services.render``), so any ``{% %}`` or markup it produced could do more than show
words. It returns parts (subject, heading, paragraphs, a button...), ``clean_parts`` checks them,
and ``build`` escapes every piece of text into the same inline-styled layout as the starter
templates. Only the listed ``{{ name }}`` blanks survive.
"""
from __future__ import annotations

import re
from html import escape

ALWAYS_AVAILABLE = {"company_name"}
MAX_VARIABLES = 8
MAX_PARAGRAPHS = 6
CAPS = {"name": 80, "subject": 150, "preheader": 150, "heading": 100, "paragraph": 600,
        "button_label": 30, "sign_off": 120, "example": 120}

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TAG = re.compile(r"\{\{(.*?)\}\}", re.S)
_CODE = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.S)
_LINK = re.compile(r"(https?://|www\.)\S+", re.I)

_WRAP = "font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:0 auto;color:#12182B;"
_P = "font-size:14px;line-height:1.6;margin:0 0 16px;"
_BUTTON = ("display:inline-block;background:#FFB020;color:#12182B;font-weight:600;text-decoration:none;"
           "padding:10px 20px;border-radius:8px;font-size:14px;")


class LayoutError(ValueError):
    """The parts can't be made into an email; the message is for the owner."""


def _snake(name: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower())).strip("_")[:40]


def plain(value, cap: int) -> str:
    """Plain words only: template code, markup brackets and web addresses are removed."""
    text = _CODE.sub("", str(value or ""))
    text = text.replace("{%", "").replace("%}", "").replace("{#", "").replace("#}", "")
    text = _LINK.sub("", text.replace("<", "").replace(">", ""))
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:cap].strip()


def tags(text: str, allowed: set[str]) -> str:
    """Normalise ``{{ name }}`` blanks; a blank that isn't listed makes the draft unusable."""
    def fix(m):
        name = m.group(1).strip()
        if name not in allowed:
            raise LayoutError(f"it uses a blank, {{{{ {name} }}}}, that isn't listed")
        return "{{ " + name + " }}"

    fixed = _TAG.sub(fix, text)
    rest = _TAG.sub("", fixed)
    if "{{" in rest or "}}" in rest:
        raise LayoutError("it has a broken blank")
    return fixed


def tags_in(text: str) -> set[str]:
    return {m.strip() for m in _TAG.findall(text or "")}


def clean_parts(data: dict) -> dict:
    """Checked, capped parts, or ``LayoutError``. ``data`` is the model's JSON (or earlier parts)."""
    if not isinstance(data, dict):
        raise LayoutError("it wasn't in the expected shape")
    variables, seen = [], set()
    for v in data.get("variables") or []:
        if not isinstance(v, dict):
            continue
        name = _snake(str(v.get("name") or ""))
        if not _NAME.match(name) or name in seen or name in ALWAYS_AVAILABLE:
            continue
        seen.add(name)
        variables.append({"name": name, "example": plain(v.get("example"), CAPS["example"])})
    variables = variables[:MAX_VARIABLES]
    allowed = {v["name"] for v in variables} | ALWAYS_AVAILABLE

    button = data.get("button") if isinstance(data.get("button"), dict) else None
    if button:
        label = plain(button.get("label"), CAPS["button_label"])
        url_variable = _snake(str(button.get("url_variable") or ""))
        if not label or url_variable not in {v["name"] for v in variables}:
            button = None  # a button always links to a blank the business fills in, never a typed URL
        else:
            button = {"label": tags(label, allowed), "url_variable": url_variable}

    paragraphs = [p for p in (data.get("paragraphs") or []) if isinstance(p, str)]
    parts = {
        "name": plain(data.get("name"), CAPS["name"]).replace("{{", "").replace("}}", ""),
        "subject": tags(plain(data.get("subject"), CAPS["subject"]), allowed),
        "preheader": tags(plain(data.get("preheader"), CAPS["preheader"]), allowed),
        "heading": tags(plain(data.get("heading"), CAPS["heading"]), allowed),
        "paragraphs": [t for t in (tags(plain(p, CAPS["paragraph"]), allowed) for p in paragraphs) if t][:MAX_PARAGRAPHS],
        "button": button,
        "sign_off": tags(plain(data.get("sign_off"), CAPS["sign_off"]), allowed),
        "variables": variables,
    }
    if not parts["subject"]:
        raise LayoutError("it has no subject")
    if not parts["paragraphs"]:
        raise LayoutError("it has no message")
    return parts


def words(parts: dict) -> str:
    """Every word the reader sees, in order: what rewrites compare and facts are checked against."""
    button = parts.get("button") or {}
    lines = [parts.get("subject", ""), parts.get("preheader", ""), parts.get("heading", ""),
             *parts.get("paragraphs", []), button.get("label", ""), parts.get("sign_off", "")]
    return "\n".join(line for line in lines if line)


def build(parts: dict, account) -> dict:
    """The template's fields from checked parts: subject, text and HTML bodies, sample variables."""
    button = parts.get("button")
    text = [parts["heading"]] if parts.get("heading") else []
    text += parts["paragraphs"]
    if button:
        text.append(f"{button['label']}: {{{{ {button['url_variable']} }}}}")
    if parts.get("sign_off"):
        text.append(parts["sign_off"])

    html = [f'<div style="{_WRAP}">']
    if parts.get("preheader"):
        html.append(f'  <div style="display:none;max-height:0;overflow:hidden;">{escape(parts["preheader"])}</div>')
    if parts.get("heading"):
        html.append(f'  <h1 style="font-size:20px;margin:0 0 16px;">{escape(parts["heading"])}</h1>')
    for paragraph in parts["paragraphs"]:
        body = "<br>".join(escape(line) for line in paragraph.split("\n"))
        html.append(f'  <p style="{_P}">{body}</p>')
    if button:
        html.append(f'  <p style="margin:0 0 24px;"><a href="{{{{ {button["url_variable"]} }}}}" '
                    f'style="{_BUTTON}">{escape(button["label"])}</a></p>')
    if parts.get("sign_off"):
        html.append(f'  <p style="{_P}">{escape(parts["sign_off"])}</p>')
    html.append("</div>")

    samples = {"company_name": getattr(account, "company_name", "") or "Your business"}
    for v in parts["variables"]:
        example = v["example"] or v["name"].replace("_", " ")
        if button and v["name"] == button["url_variable"] and not example.startswith("http"):
            example = "https://example.com"
        samples[v["name"]] = example
    return {"name": parts.get("name") or "New email", "subject": parts["subject"],
            "text_body": "\n\n".join(text), "html_body": "\n".join(html), "sample_variables": samples}
