"""What happens around a Page connection for the Facebook inbox.

The Page itself is connected in Content Studio (``apps.publishing.views.fb_oauth_*``);
these hooks subscribe it to Messenger and comment webhooks, and undo that.
"""

from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

# Messenger messages, button taps, messages sent from the Page inbox, and Page feed
# (comments). Needs pages_manage_metadata (+ pages_messaging for the messaging fields).
WEBHOOK_FIELDS = "messages,messaging_postbacks,message_echoes,feed"


def subscribe_page(conn) -> bool:
    """Have the Page send Akilent its Messenger and comment webhooks. Best effort."""
    from apps.facebook.providers.meta import provider_for

    if not (conn.access_token and conn.page_id):
        return False
    ok, error = provider_for(conn).subscribe_page(WEBHOOK_FIELDS)
    conn.webhook_subscribed_at = timezone.now() if ok else None
    conn.webhook_error = "" if ok else error[:500]
    conn.save(update_fields=["webhook_subscribed_at", "webhook_error", "updated_at"])
    return ok


def on_page_connected(conn) -> None:
    """A Page was (re)connected: subscribe it, and give it the recommended rules."""
    subscribe_page(conn)
    from apps.instagram.api import add_default_moderation_rules

    try:
        add_default_moderation_rules(
            conn.account, only_if_none=True, channel="facebook"
        )
    except Exception:
        logger.exception("Couldn't add default Facebook rules for %s", conn.account_id)


def disconnect_page(conn, *, unsubscribe: bool) -> None:
    """Stop using the Page without losing its conversations or comments."""
    from apps.facebook.providers.meta import provider_for
    from apps.publishing.models import FacebookPageConnection

    if unsubscribe and conn.access_token and conn.page_id:
        provider_for(conn).unsubscribe_page()
    conn.status = FacebookPageConnection.Status.DISCONNECTED
    conn.access_token = None
    conn.webhook_subscribed_at = None
    conn.save(
        update_fields=["status", "access_token", "webhook_subscribed_at", "updated_at"]
    )


def inbox_status(conn) -> str:
    """ "ready", "reconnect" (connected before Messenger was added, or the subscription
    failed), "expired" or "none" — for the Channels page."""
    from apps.publishing.models import FacebookPageConnection

    if conn is None or conn.status == FacebookPageConnection.Status.DISCONNECTED:
        return "none"
    if conn.status == FacebookPageConnection.Status.EXPIRED or not conn.access_token:
        return "expired"
    return "ready" if conn.webhook_subscribed_at else "reconnect"
