from django.urls import path

from . import views

app_name = "hubspot"

urlpatterns = [
    path("oauth/connect/", views.oauth_connect, name="oauth-connect"),
    path("oauth/callback/", views.oauth_callback, name="oauth-callback"),
    path("oauth/disconnect/", views.oauth_disconnect, name="oauth-disconnect"),
    path("webhook/", views.webhook, name="webhook"),
]
