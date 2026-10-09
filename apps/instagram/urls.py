from django.urls import path

from . import rules_views
from .views import (
    InstagramWebhookView,
    instagram_account_connect,
    instagram_account_delete,
    instagram_account_edit,
    instagram_accounts,
    instagram_connect_oauth_callback,
    instagram_connect_oauth_select,
    instagram_connect_oauth_start,
    instagram_data_deletion,
    instagram_deauthorize,
    instagram_outbound_media,
)

urlpatterns = [
    # Comment rules: automatic private replies + moderation
    path("rules/", rules_views.comment_rules, name="instagram-comment-rules"),
    path(
        "rules/moderation/recommended/",
        rules_views.comment_rule_defaults,
        name="instagram-comment-rule-defaults",
    ),
    path(
        "rules/<str:kind>/new/",
        rules_views.comment_rule_edit,
        name="instagram-comment-rule-new",
    ),
    path(
        "rules/<str:kind>/<int:pk>/edit/",
        rules_views.comment_rule_edit,
        name="instagram-comment-rule-edit",
    ),
    path(
        "rules/<str:kind>/<int:pk>/toggle/",
        rules_views.comment_rule_toggle,
        name="instagram-comment-rule-toggle",
    ),
    path(
        "rules/<str:kind>/<int:pk>/delete/",
        rules_views.comment_rule_delete,
        name="instagram-comment-rule-delete",
    ),
    path("webhook/", InstagramWebhookView.as_view(), name="instagram-webhook"),
    # Meta callbacks, entered in Instagram > API setup with Instagram Login >
    # Business login settings.
    path("deauthorize/", instagram_deauthorize, name="instagram-deauthorize"),
    # Signed, expiring links Meta fetches outbound attachments from.
    path(
        "media/<str:token>/",
        instagram_outbound_media,
        name="instagram-outbound-media",
    ),
    path("data-deletion/", instagram_data_deletion, name="instagram-data-deletion"),
    path("accounts/", instagram_accounts, name="instagram-accounts"),
    # OAuth (Business Login) connect flow
    path(
        "accounts/connect/oauth/",
        instagram_connect_oauth_start,
        name="instagram-connect-oauth-start",
    ),
    path(
        "accounts/connect/oauth/callback/",
        instagram_connect_oauth_callback,
        name="instagram-connect-oauth-callback",
    ),
    path(
        "accounts/connect/oauth/select/",
        instagram_connect_oauth_select,
        name="instagram-connect-oauth-select",
    ),
    # Manual / operator connect flow
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
