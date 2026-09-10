from django.urls import path

from apps.automation import views

app_name = "automation"

urlpatterns = [
    path("", views.workflow_list, name="list"),
    path("create/", views.workflow_create, name="create"),
    path("<slug:slug>/", views.workflow_editor, name="editor"),
    path("<slug:slug>/save/", views.workflow_save, name="save"),
    path("<slug:slug>/publish/", views.workflow_publish, name="publish"),
    path("<slug:slug>/archive/", views.workflow_archive, name="archive"),
    path("<slug:slug>/delete/", views.workflow_delete, name="delete"),
]
