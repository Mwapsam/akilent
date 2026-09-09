from django.urls import path

from apps.logs import views

app_name = "logs"

urlpatterns = [
    path("messages/", views.message_list, name="messages"),
    path("messages/<str:public_id>/", views.message_detail, name="message-detail"),
    path("requests/", views.request_list, name="requests"),
    path("requests/<str:public_id>/", views.request_detail, name="request-detail"),
]
