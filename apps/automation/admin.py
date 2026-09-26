from django.contrib import admin

from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun
from apps.core.admin_readonly import ReadOnlyAdmin


@admin.register(Workflow)
class WorkflowAdmin(ReadOnlyAdmin):
    list_display = ("name", "account", "status", "version", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "slug", "account__company_name")


class StepRunInline(admin.TabularInline):
    model = WorkflowStepRun
    extra = 0
    can_delete = False
    readonly_fields = ("step_id", "step_type", "status", "result", "executed_at", "outbound_message")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WorkflowRun)
class WorkflowRunAdmin(ReadOnlyAdmin):
    list_display = ("public_id", "workflow", "status", "current_step", "next_due_at", "started_at")
    list_filter = ("status",)
    search_fields = ("public_id", "workflow__name", "workflow__account__company_name")
    inlines = [StepRunInline]
