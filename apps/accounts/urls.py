from django.urls import path

from . import settings_views, views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("signup/", views.signup, name="signup"),
    path("verify/<uidb64>/<token>/", views.verify_email, name="verify_email"),
    path("resend-verification/", views.resend_verification, name="resend-verification"),
    path("onboarding/", views.onboarding, name="onboarding"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("channels/", views.channels, name="channels"),

    # Self-service account settings (profile / security / team).
    path("settings/", settings_views.settings_profile, name="settings-profile"),
    path("settings/security/", settings_views.settings_security, name="settings-security"),
    path("settings/team/", settings_views.settings_team, name="settings-team"),
    path("settings/tools/", settings_views.settings_tools, name="settings-tools"),
    path("settings/team/invite/", settings_views.invite_create, name="invite-create"),
    path("settings/team/invitations/<int:pk>/revoke/", settings_views.invite_revoke, name="invite-revoke"),
    path("settings/team/members/<int:pk>/role/", settings_views.member_role, name="member-role"),
    path("settings/team/members/<int:pk>/remove/", settings_views.member_remove, name="member-remove"),

    # Tokened invite-accept link (reachable signed in or out).
    path("invite/<str:token>/", settings_views.accept_invitation, name="accept-invitation"),
]
