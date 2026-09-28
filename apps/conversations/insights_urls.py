from django.urls import path

from apps.conversations import insights_views

# Unnamespaced, like apps.accounts.urls' dashboard — {% url 'insights' %} works from any
# template with no namespace prefix.
urlpatterns = [
    path("", insights_views.insights, name="insights"),
]
