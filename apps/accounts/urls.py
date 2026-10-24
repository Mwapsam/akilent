from django.urls import path

from apps.ai import views as ai_views

from . import settings_views, views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("signup/", views.signup, name="signup"),
    path("for/<slug:slug>/", views.use_case, name="use-case"),
    path("verify/<uidb64>/<token>/", views.verify_email, name="verify_email"),
    path("resend-verification/", views.resend_verification, name="resend-verification"),
    path("onboarding/", views.onboarding, name="onboarding"),
    path(
        "onboarding/next/<slug:key>/dismiss/",
        views.next_action_dismiss,
        name="next-action-dismiss",
    ),
    path("dashboard/", views.dashboard, name="dashboard"),
    # Dashboard fragments (HTMX). The work queue is polled; the secondary
    # panels are lazy-loaded so their aggregates stay off the first byte.
    path(
        "dashboard/work-queue/", views.dashboard_work_queue, name="dashboard-work-queue"
    ),
    path("dashboard/panels/", views.dashboard_panels, name="dashboard-panels"),
    path("channels/", views.channels, name="channels"),
    # Self-service account settings (profile / security / team).
    path("settings/", settings_views.settings_profile, name="settings-profile"),
    path(
        "settings/business/", settings_views.settings_business, name="settings-business"
    ),
    path("settings/security/2fa/", views._2fa_setup, name="2fa-setup"),
    path(
        "settings/security/2fa/recovery-codes/",
        views.recovery_codes_page,
        name="settings-recovery-codes",
    ),
    path(
        "settings/security/2fa/recovery-codes/confirm/",
        views.recovery_codes_confirm,
        name="recovery-codes-confirm",
    ),
    path(
        "settings/security/2fa/recovery-codes/regenerate/",
        views.recovery_codes_regenerate,
        name="recovery-codes-regenerate",
    ),
    path(
        "settings/security/email-codes/",
        views.email_mfa_settings,
        name="settings-email-mfa",
    ),
    path(
        "settings/security/passkeys/",
        views.passkey_settings,
        name="settings-passkeys",
    ),
    path(
        "settings/security/passkeys/reauth/",
        views.passkey_reauth_send,
        name="passkey-reauth-send",
    ),
    path(
        "settings/security/passkeys/options/",
        views.passkey_register_options,
        name="passkey-register-options",
    ),
    path(
        "settings/security/passkeys/finish/",
        views.passkey_register_finish,
        name="passkey-register-finish",
    ),
    path(
        "settings/security/passkeys/<int:pk>/remove/",
        views.passkey_remove,
        name="passkey-remove",
    ),
    path(
        "settings/security/", settings_views.settings_security, name="settings-security"
    ),
    path("settings/team/", settings_views.settings_team, name="settings-team"),
    path("settings/tools/", settings_views.settings_tools, name="settings-tools"),
    path(
        "settings/integrations/",
        settings_views.settings_integrations,
        name="settings-integrations",
    ),
    path(
        "settings/api-keys/",
        settings_views.settings_api_keys,
        name="settings-api-keys",
    ),
    path(
        "settings/api-keys/<int:pk>/revoke/",
        settings_views.api_key_revoke,
        name="settings-api-key-revoke",
    ),
    path("settings/hours/", settings_views.settings_hours, name="settings-hours"),
    path("settings/ai/", ai_views.settings_ai, name="settings-ai"),
    path(
        "settings/ai/autopilot/",
        ai_views.settings_ai_autopilot,
        name="settings-ai-autopilot",
    ),
    path(
        "settings/ai/playground/",
        ai_views.settings_ai_playground,
        name="settings-ai-playground",
    ),
    path(
        "settings/ai/knowledge/",
        ai_views.settings_ai_knowledge,
        name="settings-ai-knowledge",
    ),
    path(
        "settings/ai/knowledge/<int:pk>/toggle/",
        ai_views.knowledge_entry_toggle,
        name="settings-ai-knowledge-toggle",
    ),
    path(
        "settings/ai/knowledge/<int:pk>/delete/",
        ai_views.knowledge_entry_delete,
        name="settings-ai-knowledge-delete",
    ),
    path(
        "settings/ai/knowledge/<int:pk>/edit/",
        ai_views.knowledge_entry_edit,
        name="settings-ai-knowledge-edit",
    ),
    path(
        "settings/ai/knowledge/approve-all/",
        ai_views.knowledge_approve_all,
        name="settings-ai-knowledge-approve-all",
    ),
    path(
        "settings/ai/knowledge/import/",
        ai_views.knowledge_import,
        name="settings-ai-knowledge-import",
    ),
    path(
        "settings/ai/knowledge/website/",
        ai_views.knowledge_website,
        name="settings-ai-knowledge-website",
    ),
    path("settings/team/invite/", settings_views.invite_create, name="invite-create"),
    path(
        "settings/team/invitations/<int:pk>/revoke/",
        settings_views.invite_revoke,
        name="invite-revoke",
    ),
    path(
        "settings/team/members/<int:pk>/role/",
        settings_views.member_role,
        name="member-role",
    ),
    path(
        "settings/team/members/<int:pk>/remove/",
        settings_views.member_remove,
        name="member-remove",
    ),
    # Tokened invite-accept link (reachable signed in or out).
    path(
        "invite/<str:token>/",
        settings_views.accept_invitation,
        name="accept-invitation",
    ),
]
