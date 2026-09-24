"""Unsubscribe token generation and RFC 8058 List-Unsubscribe headers."""
from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from django.conf import settings
from django.urls import reverse

if TYPE_CHECKING:
    from apps.accounts.models import Account
    from apps.email.models import BulkEmailCampaign

logger = logging.getLogger(__name__)


def create_unsubscribe_token(
    account: "Account",
    email: str,
    campaign: "BulkEmailCampaign | None" = None,
) -> str:
    """Create a one-time unsubscribe token for a recipient and return it.

    Use :func:`get_unsubscribe_url` for the full link, or
    :func:`build_list_unsubscribe_headers` for the mail headers.
    """
    from apps.email.models import UnsubscribeToken

    # token column is max_length=64; "unsub_" (6) + token_urlsafe(32) (~43) fits.
    token = "unsub_" + secrets.token_urlsafe(32)
    UnsubscribeToken.objects.create(
        token=token,
        account=account,
        email=email,
        campaign=campaign,
    )
    return token


def _absolute_base() -> str:
    """Scheme + host for building absolute links from a Celery task (no request)."""
    base_domain = getattr(settings, "BASE_DOMAIN", "") or (
        settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else "localhost"
    )
    scheme = "http" if settings.DEBUG else "https"
    return f"{scheme}://{base_domain}"


def get_unsubscribe_url(token: str, request=None) -> str:
    """Absolute unsubscribe URL for a token.

    ``request`` is optional — when omitted (Celery tasks) the URL is built from
    ``settings.BASE_DOMAIN``.
    """
    path = reverse("email-unsubscribe", args=[token])
    if request is not None:
        return request.build_absolute_uri(path)
    return f"{_absolute_base()}{path}"


def get_unsubscribe_header(token: str, request=None) -> str:
    """The ``List-Unsubscribe`` header value for a single https link."""
    return f"<{get_unsubscribe_url(token, request)}>"


def build_unsubscribe_context(
    account: "Account",
    email: str,
    *,
    campaign: "BulkEmailCampaign | None" = None,
    campaign_id: int | None = None,
) -> dict:
    """Mint one token and return everything a send needs from it.

    Returns ``{"token", "url", "headers"}``. One token backs both the RFC 8058
    headers and the body footer link, so a recipient who clicks either lands on
    the same row.
    """
    if campaign is None and campaign_id is not None:
        from apps.email.models import BulkEmailCampaign

        campaign = BulkEmailCampaign.objects.filter(pk=campaign_id).first()

    token = create_unsubscribe_token(account, email, campaign=campaign)
    https = get_unsubscribe_url(token)
    mailto = getattr(settings, "DEFAULT_FROM_EMAIL", "") or "unsubscribe@localhost"
    return {
        "token": token,
        "url": https,
        "headers": {
            "List-Unsubscribe": f"<{https}>, <mailto:{mailto}?subject=unsubscribe>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }


def build_list_unsubscribe_headers(
    account: "Account",
    email: str,
    *,
    campaign: "BulkEmailCampaign | None" = None,
    campaign_id: int | None = None,
) -> dict[str, str]:
    """Mint a token and return the RFC 8058 one-click unsubscribe headers.

    Returns both ``List-Unsubscribe`` (https + mailto) and
    ``List-Unsubscribe-Post`` so Gmail/Yahoo (2024+) show a one-click
    unsubscribe control. The https endpoint accepts the POST; the mailto is a
    fallback for clients that don't do one-click.

    Callers that also need the link (to build a body footer) should use
    :func:`build_unsubscribe_context` instead, so only one token is minted.
    """
    return build_unsubscribe_context(
        account, email, campaign=campaign, campaign_id=campaign_id
    )["headers"]


def apply_unsubscribe(
    *,
    account: "Account",
    email: str,
    campaign: "BulkEmailCampaign | None" = None,
    source: str = "link",
) -> None:
    """Honor an unsubscribe everywhere it needs to be visible.

    Shared by the footer link (GET) and the RFC 8058 one-click POST. Writes:

    * the account suppression entry, which is what actually blocks future sends;
    * the Contact's consent fields and ``status``, so the UI, segments and
      exports stop showing the recipient as subscribed;
    * a ``MessageEvent`` so stats and outbound webhooks see the opt-out.

    Everything past the suppression entry is best-effort — a missing Contact or
    a stats failure must never stop us honoring the request.
    """
    from apps.email.services.suppression import record_event

    record_event(account=account, email=email, reason="unsubscribe")

    try:
        from apps.contacts.models import Contact
        from apps.contacts.services import record_contact_event

        contact = Contact.objects.filter(account=account, email__iexact=email).first()
        if contact is not None:
            contact.record_opt_out(f"email_unsubscribe:{source}")
            # record_contact_event owns the status flip and the
            # contact.unsubscribed webhook; record_opt_out deliberately emits
            # no event of its own so this is the only one.
            record_contact_event(
                contact,
                "email.unsubscribed",
                data={
                    "source": source,
                    "campaign": campaign.pk if campaign else None,
                },
            )
    except Exception:
        logger.exception("apply_unsubscribe: contact update failed for %s", email)

    if campaign is not None:
        try:
            from apps.email.models import EmailMessage
            from apps.logs.services import record_message_event

            msg = (
                EmailMessage.objects.filter(
                    account=account, to_email__iexact=email, campaign=campaign
                )
                .order_by("-id")
                .first()
            )
            if msg is not None:
                record_message_event(
                    msg, "unsubscribed", source="tracking_link", data={"to": email}
                )
        except Exception:
            logger.exception("apply_unsubscribe: event recording failed for %s", email)
