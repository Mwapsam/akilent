"""Public API of apps.platform_mail for other apps (the Operator Console, accounts)."""

from apps.platform_mail.models import (  # noqa: F401
    MarketingCampaign,
    MarketingList,
    MarketingListMember,
    MarketingSend,
    MarketingSettings,
    MarketingSubscriber,
)
from apps.platform_mail.services import consent, lists, render, sending  # noqa: F401
