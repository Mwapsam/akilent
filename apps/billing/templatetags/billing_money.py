from django import template

from apps.billing.money import SYMBOLS, billing_currency, format_money, format_price

register = template.Library()


@register.filter
def money(amount, currency=None):
    """19 -> "$19.00" in the platform billing currency (or ``currency``)."""
    return format_money(amount, currency or None)


@register.filter
def price(amount, currency=None):
    """A price on a pricing card: 194 -> "$194", 11.9 -> "$11.90" (money.format_price)."""
    return format_price(amount, currency or None)


@register.simple_tag
def currency_symbol():
    code = billing_currency()
    return SYMBOLS.get(code, f"{code} ")


@register.simple_tag
def currency_code():
    """The ISO code prices are charged in ("USD"), for "Prices in USD" copy."""
    return billing_currency()


@register.simple_tag
def campaign_hold_hours():
    """How long a campaign message held by the plan's limit waits before it's dropped
    (apps.whatsapp.campaigns.cancel_stale_campaign_holds), for copy that says so."""
    from django.conf import settings

    return getattr(settings, "WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS", 72)
