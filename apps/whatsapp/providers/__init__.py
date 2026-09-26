"""WhatsApp provider factory.

Resolves the configured backend at runtime via Django settings.
Currently only Meta Cloud API is supported; additional providers
can be added by implementing WhatsAppProvider ABC.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

from apps.whatsapp.providers.base import WhatsAppProvider, WhatsAppProviderError
from apps.whatsapp.providers.meta import MetaCloudAPIProvider

_ALIASES: dict[str, str] = {
    "meta": "apps.whatsapp.providers.meta.MetaCloudAPIProvider",
}


def get_whatsapp_provider(account) -> WhatsAppProvider:
    from apps.whatsapp.models import WhatsAppBusinessNumber
    from apps.core.models import Configurations

    # The account's sending number: a fully registered one (see ``is_ready``) before any other,
    # oldest first. Never an unordered ``.first()``: with two active numbers (say an old test
    # number and the approved one) PostgreSQL may return either, and the order changes after a
    # row update, so sends would suddenly come from the wrong number (e.g. Meta error 131037,
    # "display name not approved", from a number that was never approved).
    numbers = list(WhatsAppBusinessNumber.objects.filter(account=account, is_active=True).order_by("created_at", "pk"))
    number = next((n for n in numbers if n.is_ready), numbers[0] if numbers else None)
    if len(numbers) > 1:
        logger.info("account %s has %d active WhatsApp numbers; sending from %s",
                    account.slug, len(numbers), number.phone_number_id)

    if not number:
        raise WhatsAppProviderError(
            f"No active WhatsApp Business Number configured for account {account.slug}"
        )

    access_token = number.access_token

    if not access_token:
        # Fall back to a manually-entered credential in Configurations
        access_token = (
            Configurations.objects.filter(name="whatsapp_access_token")
            .values_list("value", flat=True)
            .first()
        )

    if not access_token:
        raise WhatsAppProviderError(
            f"Business number {number.phone_number_id} is missing access token"
        )

    return MetaCloudAPIProvider(
        access_token=access_token,
        phone_number_id=number.phone_number_id,
        app_id=settings.WHATSAPP_APP_ID,
    )


__all__ = [
    "get_whatsapp_provider",
    "WhatsAppProvider",
    "WhatsAppProviderError",
    "MetaCloudAPIProvider",
]