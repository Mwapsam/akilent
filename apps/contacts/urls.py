from django.urls import path

from apps.contacts import views

app_name = "contacts"

urlpatterns = [
    path("", views.contact_list, name="list"),
    path("<str:public_id>/", views.contact_detail, name="detail"),
]
