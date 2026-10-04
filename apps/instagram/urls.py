from django.urls import path

from .views import InstagramWebhookView

urlpatterns = [
    path("webhook/", InstagramWebhookView.as_view(), name="instagram-webhook"),
]
