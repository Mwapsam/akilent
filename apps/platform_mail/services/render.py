"""Turn a campaign into the email one subscriber receives."""

from __future__ import annotations

import re
from dataclasses import dataclass

import markdown
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import escape
from django.utils.safestring import mark_safe

from apps.platform_mail.models import MarketingCampaign, MarketingSettings

# Mail clients ignore stylesheets, so each tag the Markdown can produce gets inline styles.
_STYLES = {
    "p": "margin:0 0 16px;font-size:16px;line-height:1.6;color:#12182B;",
    "h2": "margin:24px 0 12px;font-size:19px;line-height:1.35;font-weight:700;color:#12182B;",
    "h3": "margin:20px 0 8px;font-size:16px;line-height:1.4;font-weight:700;color:#12182B;",
    "ul": "margin:0 0 16px;padding-left:22px;font-size:16px;line-height:1.6;color:#12182B;",
    "ol": "margin:0 0 16px;padding-left:22px;font-size:16px;line-height:1.6;color:#12182B;",
    "li": "margin:0 0 6px;",
    "a": "color:#12182B;text-decoration:underline;font-weight:600;",
    "strong": "font-weight:700;",
    "blockquote": "margin:0 0 16px;padding:4px 0 4px 14px;border-left:3px solid #FFB020;color:#46516B;",
}
_OPEN_TAG = re.compile(r"<({})(\s[^>]*)?>".format("|".join(_STYLES)))

# Unsubscribe token used by previews and test sends; it matches no subscriber.
PREVIEW_TOKEN = "test-preview"

# Stands in for each recipient's token in a campaign rendered once per run (see Prepared).
# URL-safe and HTML-safe, so it survives reverse() and escaping unchanged.
_TOKEN_SLOT = "PMTOKENSLOT7f3a9c"

_SAFE_SCHEMES = {"http", "https", "mailto"}
_LINK_ATTR = re.compile(r'<(a|img)\b([^>]*?)\s(href|src)="([^"]*)"')


def _safe_url(raw: str, tag: str) -> bool:
    from html import unescape
    from urllib.parse import urlsplit

    url = unescape(raw).strip()
    scheme = urlsplit(url).scheme.lower()
    allowed = {"http", "https"} if tag == "img" else _SAFE_SCHEMES
    return scheme in allowed


def _drop_unsafe_links(html: str) -> str:
    """Keep only http(s)/mailto links and http(s) images. Anything else (javascript:,
    data:, relative paths that mean nothing in an inbox) loses its href/src and shows as
    plain text."""

    def fix(m):
        tag, before, _attr, url = m.groups()
        if _safe_url(url, tag):
            return m.group(0)
        return f"<{tag}{before}"

    return _LINK_ATTR.sub(fix, html)


def body_html(source: str) -> str:
    """Markdown -> inline-styled HTML. Raw HTML in the source is escaped, not rendered, and
    links are limited to http, https and mailto."""
    # escape() also turns Markdown's line-start ">" quote marker into "&gt;"; put that back
    # (a lone ">" can't open a tag).
    escaped = re.sub(r"(?m)^(\s*)&gt;", r"\1>", str(escape(source)))
    html = _drop_unsafe_links(markdown.markdown(escaped, extensions=["sane_lists"]))
    html = _OPEN_TAG.sub(
        lambda m: f'<{m.group(1)}{m.group(2) or ""} style="{_STYLES[m.group(1)]}">',
        html,
    )
    return html


def unsubscribe_url(token: str) -> str:
    from apps.email.services.html_email import site_email_context

    return site_email_context()["base_url"] + reverse(
        "platform-mail-unsubscribe", kwargs={"token": token}
    )


@dataclass(frozen=True)
class Prepared:
    """A campaign rendered once, with a slot for each recipient's unsubscribe token."""

    subject: str
    text_body: str
    html_body: str
    from_name: str
    unsubscribe_slot_url: str

    def for_token(self, token: str) -> tuple[str, str, str, str]:
        """(subject, text_body, html_body, unsubscribe_url) for one recipient."""
        return (
            self.subject,
            self.text_body.replace(_TOKEN_SLOT, token),
            self.html_body.replace(_TOKEN_SLOT, token),
            self.unsubscribe_slot_url.replace(_TOKEN_SLOT, token),
        )


def prepare(campaign: MarketingCampaign) -> Prepared:
    """Render the campaign once (Markdown, template, settings) for a whole send run."""
    from apps.email.services.html_email import site_email_context

    settings_row = MarketingSettings.load()
    unsub = unsubscribe_url(_TOKEN_SLOT)
    ctx = {
        **site_email_context(),
        "subject": campaign.subject,
        "preheader": campaign.preheader,
        "heading": campaign.heading,
        # body_html escapes all raw HTML before Markdown runs and keeps only
        # http/https/mailto links (pinned by test_body_markdown_escapes_html and
        # test_only_http_https_and_mailto_links_survive).
        "body": mark_safe(body_html(campaign.body)),  # nosec B703 B308
        "cta_url": campaign.cta_url,
        "cta_label": campaign.cta_label or "Learn more",
        "unsubscribe_url": unsub,
        "postal_address": settings_row.postal_address,
    }
    html = render_to_string("platform_mail/campaign_email.html", ctx)

    parts = []
    if campaign.heading:
        parts.append(campaign.heading)
    parts.append(campaign.body.strip())
    if campaign.cta_url:
        parts.append(f"{campaign.cta_label or 'Learn more'}: {campaign.cta_url}")
    parts.append(
        "—\nYou're receiving this because you signed up for product updates from "
        f"{ctx['site_name']}.\nUnsubscribe: {unsub}"
    )
    if settings_row.postal_address:
        parts.append(settings_row.postal_address.strip())
    return Prepared(
        subject=campaign.subject,
        text_body="\n\n".join(parts) + "\n",
        html_body=html,
        from_name=settings_row.from_name,
        unsubscribe_slot_url=unsub,
    )


def render(campaign: MarketingCampaign, *, token: str) -> tuple[str, str, str]:
    """(subject, text_body, html_body) for one recipient (previews and test sends)."""
    subject, text, html, _ = prepare(campaign).for_token(token)
    return subject, text, html
