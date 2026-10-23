"""Which payment providers exist (settings.BILLING_PROVIDERS) and which one is active
(SiteSettings.billing_provider, chosen by the operator)."""

from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from .base import PaymentProvider

_providers: dict[str, PaymentProvider] | None = None


def _load() -> dict[str, PaymentProvider]:
    global _providers
    if _providers is None:
        loaded = {}
        for path in getattr(settings, "BILLING_PROVIDERS", []):
            provider = import_string(path)()
            loaded[provider.code] = provider
        _providers = loaded
    return _providers


def register(provider: PaymentProvider) -> None:
    """Add (or replace) a provider at runtime; tests use this for a fake provider."""
    _load()[provider.code] = provider


def unregister(code: str) -> None:
    _load().pop(code, None)


def reset() -> None:
    global _providers
    _providers = None


def get(code: str) -> PaymentProvider | None:
    return _load().get(code or "")


def all_providers() -> list[PaymentProvider]:
    return list(_load().values())


def selectable() -> list[PaymentProvider]:
    """Providers the operator can make the active one."""
    return [p for p in _load().values() if p.selectable]


def active() -> PaymentProvider | None:
    """The provider new checkouts and renewals use, or None when it's unknown or not set up."""
    from apps.core.models import SiteSettings

    provider = get(SiteSettings.load().billing_provider)
    if provider is None or not provider.selectable or not provider.is_configured():
        return None
    return provider


def manual_enabled() -> bool:
    """Bank transfer is an extra the operator switches on alongside the active provider."""
    from apps.billing.models import PaymentMethod

    return (
        get("manual") is not None
        and PaymentMethod.objects.filter(code="manual", is_enabled=True).exists()
    )
