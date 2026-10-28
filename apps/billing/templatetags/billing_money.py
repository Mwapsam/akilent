from django import template

from apps.billing.money import SYMBOLS, billing_currency, format_money

register = template.Library()


@register.filter
def money(amount, currency=None):
    """19 -> "$19.00" in the platform billing currency (or ``currency``)."""
    return format_money(amount, currency or None)


@register.simple_tag
def currency_symbol():
    code = billing_currency()
    return SYMBOLS.get(code, f"{code} ")


@register.simple_tag
def campaign_hold_hours():
    """How long a campaign message held by the plan's limit waits before it's dropped
    (apps.whatsapp.campaigns.cancel_stale_campaign_holds), for copy that says so."""
    from django.conf import settings

    return getattr(settings, "WHATSAPP_CAMPAIGN_HOLD_MAX_HOURS", 72)
