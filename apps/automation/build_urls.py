from django.urls import path

from apps.automation import build_views as views

app_name = "build"

urlpatterns = [
    path("", views.build_home, name="home"),
    path("profile/", views.build_profile, name="profile"),
    path("import/templates/", views.build_import_templates, name="import-templates"),
    path("dismiss/", views.build_dismiss, name="dismiss"),
    path("review/", views.build_review, name="review"),
]
