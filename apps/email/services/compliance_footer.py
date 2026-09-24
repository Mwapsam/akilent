"""CAN-SPAM compliance footer for bulk/marketing email.

Every campaign message must carry a conspicuous unsubscribe link and the
sender's physical postal address in the *body* — the RFC 8058 List-Unsubscribe
headers alone don't satisfy CAN-SPAM, and mailbox providers (and AWS SES
reviewers) look at the rendered message.

Injection happens at send time in apps.email.tasks._send_email_message, after
click-tracking rewrites, so the unsubscribe link is never turned into a tracked
redirect.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils.html import escape

if TYPE_CHECKING:
    from apps.accounts.models import Account

# Presence of this marker means a footer was already appended; append_footer is
# idempotent so a retried send can't stack two footers.
FOOTER_MARKER = "<!--akilent-compliance-footer-->"

_ADDRESS_FIELDS = (
    "address_line1",
    "address_line2",
    "city",
    "state_region",
    "postal_code",
    "country",
)


def format_postal_address(account: "Account") -> str:
    """The account's mailing address as a single comma-joined line.

    Blank fields are skipped, so a partial address still renders sensibly.
    """
    parts = [
        str(getattr(account, field, "") or "").strip() for field in _ADDRESS_FIELDS
    ]
    return ", ".join(part for part in parts if part)


def has_postal_address(account: "Account") -> bool:
    """Whether the account has enough of an address to satisfy CAN-SPAM.

    Street, city and country are the minimum that identifies a real location;
    campaign creation is gated on this (see apps.email.services.bulk).
    """
    return all(
        str(getattr(account, field, "") or "").strip()
        for field in ("address_line1", "city", "country")
    )


def _sender_label(account: "Account", sender_name: str = "") -> str:
    return (
        sender_name
        or str(getattr(account, "legal_name", "") or "").strip()
        or str(getattr(account, "company_name", "") or "").strip()
    )


def build_footer(
    account: "Account", unsubscribe_url: str, *, sender_name: str = ""
) -> tuple[str, str]:
    """Return the (text, html) footer blocks for this account.

    The html block carries FOOTER_MARKER so append_footer can detect it.
    """
    sender = _sender_label(account, sender_name)
    address = format_postal_address(account)
    identity = " · ".join(part for part in (sender, address) if part)

    text = f"\n\n--\n{identity}\nUnsubscribe: {unsubscribe_url}\n"

    html = (
        f'{FOOTER_MARKER}<div style="margin-top:32px;padding-top:16px;'
        'border-top:1px solid #e5e5e5;font-family:Arial,Helvetica,sans-serif;'
        'font-size:12px;line-height:18px;color:#666666;">'
        f"<div>{escape(identity)}</div>"
        f'<div style="margin-top:8px;">'
        f'<a href="{escape(unsubscribe_url)}" style="color:#666666;'
        'text-decoration:underline;">Unsubscribe from these emails</a>'
        "</div></div>"
    )
    return text, html


def append_footer(
    text_body: str,
    html_body: str,
    *,
    account: "Account",
    unsubscribe_url: str,
    sender_name: str = "",
) -> tuple[str, str]:
    """Append the compliance footer to a message body.

    Idempotent: a body that already carries the footer is returned unchanged.
    The html block goes before </body> when there is one (same technique as the
    tracking pixel in apps.email.services.send.apply_tracking), otherwise at the
    end.
    """
    text_footer, html_footer = build_footer(
        account, unsubscribe_url, sender_name=sender_name
    )

    if html_body and FOOTER_MARKER not in html_body:
        idx = html_body.lower().rfind("</body>")
        if idx != -1:
            html_body = html_body[:idx] + html_footer + html_body[idx:]
        else:
            html_body = html_body + html_footer

    if text_body and unsubscribe_url not in text_body:
        text_body = text_body + text_footer

    return text_body, html_body
