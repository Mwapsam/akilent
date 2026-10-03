from django.urls import path

from apps.chatbot.api import views

urlpatterns = [
    path("init/", views.init, name="chatbot-api-init"),
    path("message/", views.message, name="chatbot-api-message"),
    path("identify/", views.identify, name="chatbot-api-identify"),
]
