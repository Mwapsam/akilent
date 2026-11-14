from django.urls import path

from apps.chatbot import views

app_name = "chatbot"

urlpatterns = [
    path("", views.chatbot_list, name="list"),
    path("new/", views.chatbot_create, name="create"),
    path("<int:pk>/", views.chatbot_detail, name="detail"),
    path("<int:pk>/edit/", views.chatbot_edit, name="edit"),
    path("<int:pk>/publish/", views.chatbot_publish, name="publish"),
    path("<int:pk>/preview/", views.chatbot_preview, name="preview"),
    path("<int:pk>/preview/run/", views.chatbot_preview_run, name="preview-run"),
    path("<int:pk>/allow-site/", views.chatbot_allow_site, name="allow-site"),
    path("<int:pk>/analytics/", views.chatbot_analytics, name="analytics"),
]
