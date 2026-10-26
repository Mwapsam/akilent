"""The Facebook Page a business connected (``publishing.FacebookPageConnection``).

Messenger and Page comments use the same Page connection and Page token as Content
Studio publishing; this app only adds the inbox records around it.
"""


class TenantResolutionError(Exception):
    """Raised when a webhook entry's Page id belongs to no connected business."""


def get_page_for_webhook(page_id: str):
    """The active ``FacebookPageConnection`` for a webhook entry's Page id."""
    from apps.publishing.models import FacebookPageConnection

    conn = (
        FacebookPageConnection.objects.select_related("account")
        .filter(page_id=page_id, status=FacebookPageConnection.Status.ACTIVE)
        .exclude(page_id="")
        .first()
    )
    if conn is None or not conn.access_token:
        raise TenantResolutionError(
            f"No active Facebook Page found for page_id={page_id}."
        )
    return conn


def active_page(account):
    """The account's connected Page when it is active with a token, else None."""
    from apps.publishing.models import FacebookPageConnection

    conn = FacebookPageConnection.objects.filter(account=account).first()
    if (
        conn is None
        or conn.status != FacebookPageConnection.Status.ACTIVE
        or not conn.access_token
        or not conn.page_id
    ):
        return None
    return conn
