from django.urls import path

from apps.contacts import views

app_name = "contacts"

urlpatterns = [
    path("", views.contact_list, name="list"),
    path("create/", views.contact_create, name="create"),
    path("import/", views.contact_import, name="import"),
    path(
        "import/<int:pk>/rejected.csv",
        views.contact_import_rejected,
        name="import_rejected",
    ),
    path("bulk/add-to-list/", views.bulk_add_to_list, name="bulk_add_to_list"),
    path(
        "custom-fields/create/", views.create_custom_field, name="create_custom_field"
    ),
    path("lists/", views.list_index, name="lists"),
    path("lists/create/", views.list_create, name="list_create"),
    path("lists/<int:pk>/", views.list_detail, name="list_detail"),
    path("lists/<int:pk>/picker/", views.list_picker, name="list_picker"),
    path("lists/<int:pk>/rename/", views.list_rename, name="list_rename"),
    path("lists/<int:pk>/delete/", views.list_delete, name="list_delete"),
    path("lists/<int:pk>/add-by-tag/", views.list_add_by_tag, name="list_add_by_tag"),
    path(
        "lists/<int:pk>/add-selected/",
        views.list_add_selected,
        name="list_add_selected",
    ),
    path(
        "lists/<int:pk>/add-messaged/",
        views.list_add_messaged,
        name="list_add_messaged",
    ),
    path(
        "lists/<int:pk>/add-contact/", views.list_add_contact, name="list_add_contact"
    ),
    path(
        "lists/<int:pk>/remove-contact/",
        views.list_remove_contact,
        name="list_remove_contact",
    ),
    path("<str:public_id>/", views.contact_detail, name="detail"),
    path("<str:public_id>/edit/", views.contact_edit, name="edit"),
    path(
        "<str:public_id>/whatsapp-consent/",
        views.contact_whatsapp_consent,
        name="whatsapp_consent",
    ),
    path("<str:public_id>/tags/add/", views.contact_tag_add, name="tag_add"),
    path("<str:public_id>/tags/remove/", views.contact_tag_remove, name="tag_remove"),
]
