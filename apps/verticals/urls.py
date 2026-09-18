from django.urls import path

from apps.verticals import views

app_name = "verticals"

urlpatterns = [
    path("", views.templates, name="templates"),
    path("<str:key>/activate/", views.activate, name="activate"),
]
