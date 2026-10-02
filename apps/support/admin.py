from django.contrib import admin
from django.utils.html import format_html

from .models import (
    SLAPolicy,
    SupportCategory,
    SupportEscalation,
    SupportEvent,
    SupportInternalNote,
    SupportMessage,
    SupportQueue,
    SupportTicket,
    SupportTicketReference,
)


@admin.register(SupportCategory)
class SupportCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "parent", "is_active", "sort_order"]
    list_filter = ["is_active", "parent"]
    search_fields = ["name", "slug"]
    ordering = ["sort_order", "name"]


@admin.register(SupportQueue)
class SupportQueueAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_active", "restricted", "sort_order"]
    list_filter = ["is_active", "restricted"]


@admin.register(SLAPolicy)
class SLAPolicyAdmin(admin.ModelAdmin):
    list_display = [
        "customer_tier",
        "priority",
        "first_response_minutes",
        "update_frequency_minutes",
        "resolution_minutes",
        "escalation_after_minutes",
        "is_active",
    ]
    list_filter = ["customer_tier", "priority", "is_active"]


class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 0
    readonly_fields = ["author", "is_from_customer", "created_at"]
    fields = ["author", "is_from_customer", "body", "created_at"]


class SupportInternalNoteInline(admin.TabularInline):
    model = SupportInternalNote
    extra = 0
    readonly_fields = ["author", "created_at"]
    fields = ["author", "body", "created_at"]


class SupportEscalationInline(admin.TabularInline):
    model = SupportEscalation
    extra = 0
    readonly_fields = ["from_level", "to_level", "reason", "escalated_by", "created_at"]


class SupportEventInline(admin.TabularInline):
    model = SupportEvent
    extra = 0
    readonly_fields = ["event_type", "actor", "metadata", "created_at"]
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class SupportTicketReferenceInline(admin.TabularInline):
    model = SupportTicketReference
    extra = 0
    fields = ["content_type", "object_id", "relationship", "label"]


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = [
        "ticket_number",
        "subject",
        "account",
        "customer_tier_badge",
        "priority",
        "status",
        "support_level",
        "assigned_agent",
        "sla_breached",
        "created_at",
    ]
    list_filter = [
        "status",
        "priority",
        "customer_tier",
        "support_level",
        "sla_breached",
        "queue",
    ]
    search_fields = ["ticket_number", "subject", "account__company_name"]
    readonly_fields = ["ticket_number", "created_at", "updated_at"]
    raw_id_fields = [
        "account",
        "submitted_by",
        "assigned_agent",
        "category",
        "queue",
        "sla_policy",
    ]
    date_hierarchy = "created_at"
    inlines = [
        SupportTicketReferenceInline,
        SupportMessageInline,
        SupportInternalNoteInline,
        SupportEscalationInline,
        SupportEventInline,
    ]

    fieldsets = [
        (
            "Identity",
            {"fields": ["ticket_number", "account", "submitted_by"]},
        ),
        (
            "Classification",
            {
                "fields": [
                    "category",
                    "queue",
                    "customer_tier",
                    "priority",
                    "severity_note",
                ]
            },
        ),
        (
            "Assignment",
            {"fields": ["status", "support_level", "assigned_agent"]},
        ),
        (
            "Content",
            {"fields": ["subject", "description"]},
        ),
        (
            "SLA",
            {
                "fields": [
                    "sla_policy",
                    "sla_due_at",
                    "sla_breached",
                    "first_response_at",
                ]
            },
        ),
        (
            "Timestamps",
            {
                "fields": ["resolved_at", "closed_at", "created_at", "updated_at"],
                "classes": ["collapse"],
            },
        ),
    ]

    @admin.display(description="Tier")
    def customer_tier_badge(self, obj):
        colours = {1: "#6b7280", 2: "#3b82f6", 3: "#f59e0b", 4: "#ef4444"}
        colour = colours.get(obj.customer_tier, "#6b7280")
        return format_html(
            '<span style="color:{};font-weight:bold">T{}</span>',
            colour,
            obj.customer_tier,
        )


@admin.register(SupportMessage)
class SupportMessageAdmin(admin.ModelAdmin):
    list_display = ["ticket", "author", "is_from_customer", "created_at"]
    list_filter = ["is_from_customer"]
    raw_id_fields = ["ticket", "author"]


@admin.register(SupportEscalation)
class SupportEscalationAdmin(admin.ModelAdmin):
    list_display = [
        "ticket",
        "from_level",
        "to_level",
        "reason",
        "escalated_by",
        "created_at",
    ]
    raw_id_fields = ["ticket", "escalated_by"]


@admin.register(SupportEvent)
class SupportEventAdmin(admin.ModelAdmin):
    list_display = ["ticket", "event_type", "actor", "created_at"]
    list_filter = ["event_type"]
    raw_id_fields = ["ticket", "actor"]
