from django.contrib import admin

from apps.contacts.models import Contact
from apps.core.admin_readonly import ReadOnlyAdmin


@admin.register(Contact)
class ContactAdmin(ReadOnlyAdmin):
    list_display = ("public_id", "account", "first_name", "phone", "email", "status", "first_seen")
    list_filter = ("status",)
    search_fields = ("public_id", "phone", "email", "first_name", "account__company_name")
