from django.contrib import admin

from apps.platform_mail.models import (
    MarketingCampaign,
    MarketingList,
    MarketingSend,
    MarketingSettings,
    MarketingSubscriber,
)


@admin.register(MarketingSubscriber)
class MarketingSubscriberAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "name",
        "status",
        "source",
        "consented_at",
        "unsubscribed_at",
    )
    list_filter = ("status", "source")
    search_fields = ("email", "name")
    readonly_fields = ("token", "created_at", "updated_at")


@admin.register(MarketingList)
class MarketingListAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "created_at")


@admin.register(MarketingCampaign)
class MarketingCampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "subject", "status", "scheduled_at", "finished_at")
    list_filter = ("status",)


@admin.register(MarketingSend)
class MarketingSendAdmin(admin.ModelAdmin):
    list_display = ("campaign", "subscriber", "status", "sent_at")
    list_filter = ("status",)
    raw_id_fields = ("campaign", "subscriber")


admin.site.register(MarketingSettings)
