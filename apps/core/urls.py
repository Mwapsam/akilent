"""Operator Console routes, mounted at /manage/ (namespace ``core``)."""
from django.urls import path

from apps.billing import views as billing_views
from apps.core.console import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),

    # Businesses
    path("businesses/", views.businesses, name="businesses"),
    path("businesses/<int:pk>/", views.business_detail, name="business"),
    path("businesses/<int:pk>/tab/<slug:tab>/", views.business_detail, name="business-tab"),
    path("businesses/<int:pk>/do/<slug:action>/", views.business_action, name="business-action"),
    path("businesses/<int:pk>/export-contacts/", views.business_export_contacts, name="business-export-contacts"),
    path("businesses/<int:pk>/view-as/", views.view_as_start, name="view-as-start"),
    path("view-as/stop/", views.view_as_stop, name="view-as-stop"),

    # Payments, plans and payment methods (the actions live in apps.billing.views)
    path("payments/", views.payments, name="payments"),
    path("payments/<int:pk>/approve/", billing_views.manual_approve, name="payment-approve"),
    path("payments/<int:pk>/reject/", billing_views.manual_reject, name="payment-reject"),
    path("plans/", views.plans, name="plans"),
    path("plans/create/", billing_views.plan_create, name="plan-create"),
    path("plans/<int:pk>/edit/", billing_views.plan_edit, name="plan-edit"),
    path("plans/<int:pk>/toggle/", billing_views.plan_toggle, name="plan-toggle"),
    path("plans/<int:pk>/delete/", billing_views.plan_delete, name="plan-delete"),
    path("plans/<int:pk>/sync-fw/", billing_views.plan_sync_fw, name="plan-sync-fw"),
    path("plans/features/", views.plan_features, name="plan-features"),
    path("plans/coming-soon/", views.coming_soon_save, name="coming-soon-save"),
    path("plans/coming-soon/<int:pk>/delete/", views.coming_soon_delete, name="coming-soon-delete"),
    path("payment-methods/<int:pk>/toggle/", billing_views.payment_method_toggle, name="payment-method-toggle"),
    path("payment-methods/<int:pk>/edit/", billing_views.payment_method_edit, name="payment-method-edit"),

    # Platform
    path("pilot/", views.pilot_command_center, name="pilot"),
    path("health/", views.platform_health, name="platform-health"),
    path("audit/", views.audit_log, name="audit"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/mail/", views.mail_settings_save, name="mail-settings"),
    path("settings/users/<int:pk>/toggle-admin/", views.user_toggle_admin, name="user-toggle-admin"),
    path("settings/configurations/", views.configurations_list, name="configurations-list"),
    path("settings/configurations/create/", views.create_configuration, name="create-configuration"),
    path("settings/configurations/<int:pk>/edit/", views.edit_configuration, name="edit-configuration"),
    path("settings/configurations/<int:pk>/delete/", views.delete_configuration, name="delete-configuration"),
    path("styleguide/", views.styleguide, name="styleguide"),

    # Old addresses, kept so bookmarks and emails still work
    path("customers/", views.old_customers, name="customers"),
    path("customers/<int:pk>/", views.old_customer_detail, name="customer-detail"),
    path("billing-requests/", views.old_billing_requests, name="billing-requests"),
]
