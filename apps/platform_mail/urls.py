from django.urls import path

from apps.platform_mail import views

urlpatterns = [
    path(
        "unsubscribe/<str:token>/", views.unsubscribe, name="platform-mail-unsubscribe"
    ),
]
