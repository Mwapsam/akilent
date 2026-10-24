"""What other apps may ask Content Studio publishing, without importing its internals."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

# A reconciliation case open this long has been missed by the business; flag it to operators.
RECONCILING_ALERT_AFTER = timedelta(days=1)
FAILED_WINDOW = timedelta(hours=24)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def attention_reasons(account, now=None) -> list[str]:
    """Publishing problems an operator should see for this business, in plain words."""
    from apps.instagram.models import InstagramBusinessAccount

    from .models import ContentPostVariant, FacebookPageConnection

    now = now or timezone.now()
    variants = ContentPostVariant.objects.filter(post__account=account)
    out = []

    reconciling = variants.filter(
        status=ContentPostVariant.Status.RECONCILING,
        updated_at__lte=now - RECONCILING_ALERT_AFTER,
    ).count()
    if reconciling:
        out.append(f"{_plural(reconciling, 'post')} awaiting reconciliation for 1+ day")

    failed = variants.filter(
        status=ContentPostVariant.Status.FAILED,
        last_error_at__gte=now - FAILED_WINDOW,
    ).count()
    if failed:
        out.append(f"{_plural(failed, 'failed post publish')} today")

    if FacebookPageConnection.objects.filter(
        account=account, status=FacebookPageConnection.Status.EXPIRED
    ).exists():
        out.append("Facebook Page needs reconnecting")
    if InstagramBusinessAccount.objects.filter(
        account=account, is_active=True, token_expired=True
    ).exists():
        out.append("Instagram needs reconnecting")
    return out


def journey(account, period) -> dict | None:
    """Posts → clicks → chats → leads → orders → paid revenue for one period; see
    ``apps.publishing.results.journey``."""
    from apps.publishing.results import journey as _journey

    return _journey(account, period)
