from django.urls import path

from .views import (
    InstagramWebhookView,
    instagram_account_connect,
    instagram_account_delete,
    instagram_account_edit,
    instagram_accounts,
)

urlpatterns = [
    path("webhook/", InstagramWebhookView.as_view(), name="instagram-webhook"),
    path("accounts/", instagram_accounts, name="instagram-accounts"),
    path(
        "accounts/connect/", instagram_account_connect, name="instagram-account-connect"
    ),
    path(
        "accounts/<int:pk>/edit/", instagram_account_edit, name="instagram-account-edit"
    ),
    path(
        "accounts/<int:pk>/delete/",
        instagram_account_delete,
        name="instagram-account-delete",
    ),
]
