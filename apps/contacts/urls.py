from django.urls import path

from apps.contacts import manage_views, merge_views, segment_views, views

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
    path("bulk/tag/", manage_views.bulk_tag, name="bulk_tag"),
    path("bulk/delete/", manage_views.bulk_delete, name="bulk_delete"),
    path("export.csv", manage_views.contacts_export, name="export"),
    path("fields/", manage_views.field_index, name="fields"),
    path("fields/<int:pk>/archive/", manage_views.field_archive, name="field_archive"),
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
    path("duplicates/", merge_views.duplicates, name="duplicates"),
    path("segments/", segment_views.segment_index, name="segments"),
    path("segments/new/", segment_views.segment_form, name="segment_new"),
    path("segments/preview/", segment_views.segment_preview, name="segment_preview"),
    path("segments/<int:pk>/", segment_views.segment_form, name="segment_edit"),
    path(
        "segments/<int:pk>/delete/",
        segment_views.segment_delete,
        name="segment_delete",
    ),
    path(
        "segments/<int:pk>/to-list/",
        segment_views.segment_to_list,
        name="segment_to_list",
    ),
    path("<str:public_id>/", views.contact_detail, name="detail"),
    path("<str:public_id>/edit/", views.contact_edit, name="edit"),
    path("<str:public_id>/merge/", merge_views.contact_merge, name="merge"),
    path("<str:public_id>/delete/", manage_views.contact_delete, name="delete"),
    path("<str:public_id>/notes/", manage_views.note_add, name="note_add"),
    path(
        "<str:public_id>/notes/<int:pk>/delete/",
        manage_views.note_delete,
        name="note_delete",
    ),
    path(
        "<str:public_id>/fields/", manage_views.contact_fields_save, name="fields_save"
    ),
    path(
        "<str:public_id>/not-duplicate/",
        merge_views.contact_not_duplicate,
        name="not_duplicate",
    ),
    path(
        "<str:public_id>/whatsapp-consent/",
        views.contact_whatsapp_consent,
        name="whatsapp_consent",
    ),
    path("<str:public_id>/tags/add/", views.contact_tag_add, name="tag_add"),
    path("<str:public_id>/tags/remove/", views.contact_tag_remove, name="tag_remove"),
]
