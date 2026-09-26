from django.contrib import admin

from apps.commerce.models import Order
from apps.core.admin_readonly import ReadOnlyAdmin


@admin.register(Order)
class OrderAdmin(ReadOnlyAdmin):
    list_display = ("pk", "account", "status", "total", "currency", "paid_at")
    list_filter = ("status",)
    search_fields = ("account__company_name",)
