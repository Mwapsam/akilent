from django.urls import path

from apps.contacts import views

app_name = "contacts"

urlpatterns = [
    path("", views.contact_list, name="list"),
    path("create/", views.contact_create, name="create"),
    path("custom-fields/create/", views.create_custom_field, name="create_custom_field"),
    path("<str:public_id>/", views.contact_detail, name="detail"),
    path("<str:public_id>/edit/", views.contact_edit, name="edit"),
    path("<str:public_id>/tags/add/", views.contact_tag_add, name="tag_add"),
    path("<str:public_id>/tags/remove/", views.contact_tag_remove, name="tag_remove"),
]
