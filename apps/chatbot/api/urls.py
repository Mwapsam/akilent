from django.urls import path

from apps.chatbot.api import views

urlpatterns = [
    path("init/", views.init, name="chatbot-api-init"),
    path("message/", views.message, name="chatbot-api-message"),
    path("identify/", views.identify, name="chatbot-api-identify"),
    path("open/", views.open_chat, name="chatbot-api-open"),
    path("poll/", views.poll, name="chatbot-api-poll"),
    path("handoff/", views.handoff, name="chatbot-api-handoff"),
]
