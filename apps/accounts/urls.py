from django.urls import path

from apps.ai import views as ai_views

from . import settings_views, views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("signup/", views.signup, name="signup"),
    path("verify/<uidb64>/<token>/", views.verify_email, name="verify_email"),
    path("resend-verification/", views.resend_verification, name="resend-verification"),
    path("onboarding/", views.onboarding, name="onboarding"),
    path("dashboard/", views.dashboard, name="dashboard"),
    # Dashboard fragments (HTMX). The work queue is polled; the secondary
    # panels are lazy-loaded so their aggregates stay off the first byte.
    path("dashboard/work-queue/", views.dashboard_work_queue, name="dashboard-work-queue"),
    path("dashboard/panels/", views.dashboard_panels, name="dashboard-panels"),
    path("channels/", views.channels, name="channels"),

    # Self-service account settings (profile / security / team).
    path("settings/", settings_views.settings_profile, name="settings-profile"),
    path("settings/business/", settings_views.settings_business, name="settings-business"),
    path("settings/security/", settings_views.settings_security, name="settings-security"),
    path("settings/team/", settings_views.settings_team, name="settings-team"),
    path("settings/tools/", settings_views.settings_tools, name="settings-tools"),
    path("settings/hours/", settings_views.settings_hours, name="settings-hours"),
    path("settings/ai/", ai_views.settings_ai, name="settings-ai"),
    path("settings/ai/autopilot/", ai_views.settings_ai_autopilot, name="settings-ai-autopilot"),
    path("settings/team/invite/", settings_views.invite_create, name="invite-create"),
    path("settings/team/invitations/<int:pk>/revoke/", settings_views.invite_revoke, name="invite-revoke"),
    path("settings/team/members/<int:pk>/role/", settings_views.member_role, name="member-role"),
    path("settings/team/members/<int:pk>/remove/", settings_views.member_remove, name="member-remove"),

    # Tokened invite-accept link (reachable signed in or out).
    path("invite/<str:token>/", settings_views.accept_invitation, name="accept-invitation"),
]
