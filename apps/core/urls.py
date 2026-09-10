from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("styleguide/", views.styleguide, name="styleguide"),
    path("customers/", views.customers, name="customers"),
    path("customers/<int:pk>/toggle/", views.customer_toggle, name="customer-toggle"),
    path("customers/<int:pk>/subscription/", views.customer_subscription, name="customer-subscription"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/mail/", views.mail_settings_save, name="mail-settings"),
    path("settings/users/<int:pk>/toggle-admin/", views.user_toggle_admin, name="user-toggle-admin"),
    path("settings/configurations/", views.configurations_list, name="configurations-list"),
    path("settings/configurations/create/", views.create_configuration, name="create-configuration"),
    path("settings/configurations/<int:pk>/edit/", views.edit_configuration, name="edit-configuration"),
    path("settings/configurations/<int:pk>/delete/", views.delete_configuration, name="delete-configuration"),
]
