"""Warnings about why Meta might reject a template, before it's submitted.

``template_builder.validate_fields`` enforces what Meta *requires* (name format, numbered
variables, examples). This adds what commonly gets a template *rejected or re-classified*, in owner
words, without blocking: the owner can still submit. Used for AI drafts and hand-written templates.
"""
from __future__ import annotations

import re

_VAR = re.compile(r"\{\{\d+\}\}")
MAX_BODY, MAX_HEADER, MAX_FOOTER = 1024, 60, 60

# Wording that makes Meta treat a message as marketing.
PROMOTIONAL = (
    "discount", "offer", "sale", "promo", "promotion", "free", "% off", "limited time", "buy now",
    "shop now", "deal", "special price", "cheapest", "win", "coupon", "hurry", "don't miss",
)

# Languages Meta accepts for message templates (codes as Meta uses them).
META_LANGUAGES = {
    "af", "sq", "ar", "az", "bn", "bg", "ca", "zh_CN", "zh_HK", "zh_TW", "hr", "cs", "da", "nl", "en",
    "en_GB", "en_US", "et", "fil", "fi", "fr", "ka", "de", "el", "gu", "ha", "he", "hi", "hu", "id",
    "ga", "it", "ja", "kn", "kk", "rw_RW", "ko", "ky_KG", "lo", "lv", "lt", "mk", "ms", "ml", "mr",
    "nb", "fa", "pl", "pt_BR", "pt_PT", "pa", "ro", "ru", "sr", "sk", "sl", "es", "es_AR", "es_ES",
    "es_MX", "sw", "sv", "ta", "te", "th", "tr", "uk", "ur", "uz", "vi", "zu",
}


def lint(*, category: str, language: str, body: str, header: str = "", footer: str = "") -> list[dict]:
    """``[{"level": "warning" | "info", "text"}]``; empty means nothing likely to trip Meta up."""
    out = []

    def warn(text, level="warning"):
        out.append({"level": level, "text": text})

    body = body or ""
    stripped = body.strip()
    if _VAR.match(stripped):
        warn("The message starts with a blank ({{1}}). Meta rejects that: start with a word, e.g. “Hi {{1}}”.")
    if re.search(r"\{\{\d+\}\}\s*$", stripped):
        warn("The message ends with a blank. Meta rejects that: add a few words after it.")
    if re.search(r"\}\}\s*\{\{", body):
        warn("Two blanks sit next to each other. Put some words between them.")
    blanks = len(_VAR.findall(body))
    words = len(_VAR.sub(" ", body).split())
    if blanks and words < blanks * 3 + 2:
        warn("There are a lot of blanks for such a short message. Meta may reject it: add more fixed wording.")
    lowered = body.lower() + " " + (header or "").lower()
    if category == "utility":
        found = [w for w in PROMOTIONAL if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", lowered)]
        if found:
            warn(f"Words like “{found[0]}” sound like marketing, so Meta may change this to a Marketing "
                 "template (costs more, and only goes to customers who agreed to marketing).")
    if category == "marketing":
        warn("Marketing templates only go to customers who agreed to receive marketing messages.", "info")
    if language and language not in META_LANGUAGES:
        name = {"bem": "Bemba", "ny": "Nyanja", "loz": "Lozi", "toi": "Tonga", "sn": "Shona"}.get(language, language)
        warn(f"WhatsApp doesn't offer templates in {name}. You can still send it as a normal reply inside the "
             "24-hour window, but a template must use a supported language, such as English.")
    if len(body) > MAX_BODY:
        warn(f"The message is {len(body)} characters; WhatsApp allows {MAX_BODY}.")
    if len(header or "") > MAX_HEADER:
        warn(f"The header is longer than {MAX_HEADER} characters.")
    if len(footer or "") > MAX_FOOTER:
        warn(f"The footer is longer than {MAX_FOOTER} characters.")
    return out
