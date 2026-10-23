from django.contrib import admin

from .models import (
    AccountFeatureOverride,
    BillingPayment,
    ComingSoonFeature,
    CreditGrant,
    CreditTransaction,
    ManualPaymentRequest,
    ModuleSubscription,
    PaymentMethod,
    Plan,
    PlanFeature,
    SavedPaymentMethod,
    Subscription,
    UsageSummary,
)


@admin.register(PlanFeature)
class PlanFeatureAdmin(admin.ModelAdmin):
    list_display = ("plan", "key", "created_at")
    list_filter = ("plan",)


@admin.register(AccountFeatureOverride)
class AccountFeatureOverrideAdmin(admin.ModelAdmin):
    list_display = ("account", "key", "grant", "note", "set_by", "updated_at")
    list_filter = ("grant", "key")


@admin.register(ComingSoonFeature)
class ComingSoonFeatureAdmin(admin.ModelAdmin):
    list_display = ("name", "order", "is_active")


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "slug",
        "price_monthly",
        "max_conversations_per_month",
        "max_emails_per_month",
        "max_automation_rules",
        "max_whatsapp_numbers",
        "trial_days",
        "has_priority_support",
        "bulk_email",
        "is_active",
    ]
    list_filter = ["is_active", "has_priority_support"]
    search_fields = ["name", "slug"]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = [
        "account",
        "plan",
        "status",
        "trial_ends_at",
        "current_period_start",
        "current_period_end",
        "provider",
        "cancel_at_period_end",
        "created_at",
    ]
    list_filter = ["status", "plan", "provider"]
    search_fields = ["account__company_name", "account__slug", "fw_customer_email"]
    readonly_fields = ["created_at", "updated_at"]
    raw_id_fields = ["account"]


@admin.register(UsageSummary)
class UsageSummaryAdmin(admin.ModelAdmin):
    list_display = ["account", "period_start", "conversations_used", "emails_used"]
    list_filter = ["period_start"]
    search_fields = ["account__company_name"]
    raw_id_fields = ["account"]


@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "is_enabled", "sort_order"]
    list_filter = ["is_enabled"]
    search_fields = ["name", "code"]


@admin.register(ManualPaymentRequest)
class ManualPaymentRequestAdmin(admin.ModelAdmin):
    list_display = [
        "account",
        "plan",
        "status",
        "reference",
        "reviewed_by",
        "created_at",
    ]
    list_filter = ["status", "plan"]
    search_fields = ["account__company_name", "reference"]
    raw_id_fields = ["account"]
    readonly_fields = ["created_at"]


@admin.register(ModuleSubscription)
class ModuleSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["account", "module", "enabled", "billing_status", "created_at"]
    list_filter = ["module", "enabled", "billing_status"]
    search_fields = ["account__company_name", "account__slug"]
    raw_id_fields = ["account", "subscription"]
    readonly_fields = ["created_at", "updated_at"]
    fieldsets = (
        (
            "Account & Module",
            {"fields": ["account", "module"]},
        ),
        (
            "Status",
            {"fields": ["enabled", "billing_status"]},
        ),
        (
            "Configuration & Limits",
            {"fields": ["configuration", "limits"]},
        ),
        (
            "Subscription Link",
            {"fields": ["subscription"]},
        ),
        (
            "Audit",
            {"fields": ["created_at", "updated_at"]},
        ),
    )


class _ReadOnly(admin.ModelAdmin):
    """Money records change only through apps.billing (payments, credits) and the Operator
    console, which audit every change."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(BillingPayment)
class BillingPaymentAdmin(_ReadOnly):
    list_display = [
        "reference",
        "account",
        "plan",
        "purpose",
        "provider",
        "amount_due",
        "credit_applied",
        "currency",
        "status",
        "created_at",
    ]
    list_filter = ["status", "purpose", "provider"]
    search_fields = ["reference", "provider_ref", "account__company_name"]


@admin.register(CreditGrant)
class CreditGrantAdmin(_ReadOnly):
    list_display = [
        "account",
        "amount",
        "remaining",
        "currency",
        "reason",
        "expires_at",
        "revoked_at",
    ]
    list_filter = ["reason"]
    search_fields = ["account__company_name", "note"]


@admin.register(CreditTransaction)
class CreditTransactionAdmin(_ReadOnly):
    list_display = ["account", "kind", "amount", "payment", "created_at"]
    list_filter = ["kind"]
    search_fields = ["account__company_name"]


@admin.register(SavedPaymentMethod)
class SavedPaymentMethodAdmin(_ReadOnly):
    list_display = [
        "account",
        "provider",
        "brand",
        "last4",
        "expiry",
        "is_default",
        "created_at",
    ]
    list_filter = ["provider"]
    exclude = ["token"]
