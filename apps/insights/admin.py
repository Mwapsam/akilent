from django.contrib import admin

from apps.insights.models import BusinessPolicy, Insight, RecommendationLog


@admin.register(Insight)
class InsightAdmin(admin.ModelAdmin):
    list_display = [
        "account",
        "type",
        "severity",
        "status",
        "evidence_count",
        "created_at",
    ]
    list_filter = ["severity", "status", "type"]
    search_fields = ["account__company_name", "title", "type"]
    readonly_fields = ["created_at", "updated_at"]
    raw_id_fields = ["account"]


@admin.register(RecommendationLog)
class RecommendationLogAdmin(admin.ModelAdmin):
    list_display = ["account", "insight", "accepted", "recommended_at", "acted_at"]
    list_filter = ["accepted"]
    readonly_fields = ["recommended_at"]
    raw_id_fields = ["account", "insight"]


@admin.register(BusinessPolicy)
class BusinessPolicyAdmin(admin.ModelAdmin):
    list_display = ["name", "account", "trigger", "status", "created_at"]
    list_filter = ["trigger", "status"]
    search_fields = ["name", "account__company_name"]
    readonly_fields = ["created_at", "updated_at"]
    raw_id_fields = ["account", "created_from", "created_by"]
