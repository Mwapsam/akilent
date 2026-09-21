from django.urls import path

from apps.conversations import views

app_name = "conversations"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("feed/", views.inbox_feed, name="inbox_feed"),
    path("<str:public_id>/", views.conversation_detail, name="detail"),
    path("<str:public_id>/messages/", views.messages_feed, name="messages_feed"),
]
