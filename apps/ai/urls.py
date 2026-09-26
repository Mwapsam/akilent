from django.urls import path

from apps.ai import views

app_name = "ai"

urlpatterns = [
    path("drafts/", views.draft_create, name="draft-create"),
    path("drafts/<int:pk>/", views.draft_status, name="draft-status"),
]
