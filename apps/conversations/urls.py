from django.urls import path

from apps.conversations import views

app_name = "conversations"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("feed/", views.inbox_feed, name="inbox_feed"),
    path("followups/", views.followups_due, name="followups_due"),
    path("followups/<int:pk>/complete/", views.followup_complete, name="followup_complete"),
    path("saved-replies/", views.saved_replies, name="saved_replies"),
    path("saved-replies/<int:pk>/delete/", views.saved_reply_delete, name="saved_reply_delete"),
    path("<str:public_id>/", views.conversation_detail, name="detail"),
    path("<str:public_id>/messages/", views.messages_feed, name="messages_feed"),
    path("<str:public_id>/ai/suggest/", views.ai_suggest, name="ai_suggest"),
    path("<str:public_id>/ai/dismiss/", views.ai_dismiss, name="ai_dismiss"),
]
