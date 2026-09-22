from django.urls import path

from apps.whatsapp import numbers, views
from apps.whatsapp.views import WhatsAppWebhookView

urlpatterns = [
    path("templates/sync/", views.templates_sync, name="whatsapp-templates-sync"),
    path("campaigns/new/", views.campaign_new, name="whatsapp-campaign-new"),
    path("campaigns/<int:pk>/", views.campaign_detail, name="whatsapp-campaign-detail"),
    path("numbers/", numbers.numbers_list, name="whatsapp-numbers"),
    path("numbers/create/", numbers.numbers_create, name="whatsapp-numbers-create"),
    path("numbers/<int:pk>/delete/", numbers.numbers_delete, name="whatsapp-numbers-delete"),
    path("numbers/<int:pk>/register/", numbers.numbers_register, name="whatsapp-numbers-register"),
    path("numbers/<int:pk>/status/", numbers.numbers_status, name="whatsapp-numbers-status"),
    path("numbers/<int:pk>/verify/", numbers.numbers_verify, name="whatsapp-numbers-verify"),
    path("connect/complete/", numbers.connect_complete, name="whatsapp-connect-complete"),
    path("connect/redirect/", numbers.connect_redirect_start, name="whatsapp-connect-redirect-start"),
    path(
        "connect/redirect/callback/",
        numbers.connect_redirect_callback,
        name="whatsapp-connect-redirect-callback",
    ),
    path(
        "connect/redirect/select/",
        numbers.connect_redirect_select,
        name="whatsapp-connect-redirect-select",
    ),
    path("webhook/", WhatsAppWebhookView.as_view(), name="whatsapp-webhook"),
]
