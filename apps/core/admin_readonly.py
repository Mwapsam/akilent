"""Read-only Django admin pages for raw data.

The Operator Console is where operators act; Django admin (/admin/) is only for looking at the
raw rows behind a support question. These admins can list, search and open rows, never change
or delete them, so nothing bypasses the console's checks and audit log.
"""
from django.contrib import admin


class ReadOnlyAdmin(admin.ModelAdmin):
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
