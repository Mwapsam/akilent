from django.urls import path

from .views import FacebookWebhookView, facebook_data_deletion, facebook_deauthorize

urlpatterns = [
    path("webhook/", FacebookWebhookView.as_view(), name="facebook-webhook"),
    # Meta's Facebook Login callbacks (public; see apps.core.channels public_paths).
    path("deauthorize/", facebook_deauthorize, name="facebook-deauthorize"),
    path("data-deletion/", facebook_data_deletion, name="facebook-data-deletion"),
]
