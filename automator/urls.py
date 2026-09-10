from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include

from apps.accounts.forms import LoginForm
from apps.accounts.views import LoginView, LogoutView, PasswordResetView
from apps.core import views as core_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "auth/login/",
        LoginView.as_view(template_name="auth/login.html", authentication_form=LoginForm),
        name="login",
    ),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path(
        "auth/password-reset/",
        PasswordResetView.as_view(
            template_name="auth/password_reset.html",
            email_template_name="auth/password_reset_email.txt",
            subject_template_name="auth/password_reset_subject.txt",
            success_url="/auth/password-reset/done/",
        ),
        name="password_reset",
    ),
    path(
        "auth/password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(template_name="auth/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "auth/reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="auth/password_reset_confirm.html",
            success_url="/auth/reset/done/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "auth/reset/done/",
        auth_views.PasswordResetCompleteView.as_view(template_name="auth/password_reset_complete.html"),
        name="password_reset_complete",
    ),
    path("help/", core_views.help_index, name="help"),
    path("help/<slug:slug>/", core_views.help_article, name="help-article"),
    path("docs/", core_views.docs_page, name="docs"),
    path("docs/<slug:slug>/", core_views.docs_page, name="docs-page"),
    path("", include("apps.accounts.urls")),
    path("email/", include("apps.email.urls")),
    path("logs/", include("apps.logs.urls", namespace="logs")),
    path("contacts/", include("apps.contacts.urls", namespace="contacts")),
    path("automations/", include("apps.automation.urls", namespace="automation")),
    path("billing/", include("apps.billing.urls", namespace="billing")),
    path("manage/", include("apps.core.urls", namespace="core")),
    path("api/", include("apps.api.urls")),
]

# Soft-disabled verticals — only routed when their feature flag is on.
if settings.WHATSAPP_ENABLED:
    urlpatterns += [path("whatsapp/", include("apps.whatsapp.urls"))]

# Internal debug API — off by default; see settings.INTERNAL_DEBUG_ENABLED.
if settings.INTERNAL_DEBUG_ENABLED:
    urlpatterns += [path("internal/debug/api/", include("apps.internal_debug.urls"))]

# Serve user-uploaded media in development.
if settings.DEBUG:
    from django.conf.urls.static import static

    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
