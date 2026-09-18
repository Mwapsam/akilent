from django.urls import path

from apps.conversations import views

app_name = "conversations"

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("<str:public_id>/", views.conversation_detail, name="detail"),
]
