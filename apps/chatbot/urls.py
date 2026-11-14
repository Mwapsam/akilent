from django.urls import path

from apps.chatbot import views

app_name = "chatbot"

urlpatterns = [
    path("", views.chatbot_list, name="list"),
    path("new/", views.chatbot_create, name="create"),
    path("<int:pk>/", views.chatbot_detail, name="detail"),
    path("<int:pk>/edit/", views.chatbot_edit, name="edit"),
    path("<int:pk>/analytics/", views.chatbot_analytics, name="analytics"),
]
