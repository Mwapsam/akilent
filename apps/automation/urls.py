from django.urls import path

from apps.automation import views

app_name = "automation"

urlpatterns = [
    path("", views.workflow_list, name="list"),
    path("create/", views.workflow_create, name="create"),
    # Before the <slug> route, which would otherwise swallow it.
    path("starters/install/", views.starter_install, name="starter-install"),
    path("<slug:slug>/", views.workflow_editor, name="editor"),
    path("<slug:slug>/stats/", views.workflow_stats, name="stats"),
    path("<slug:slug>/save/", views.workflow_save, name="save"),
    path("<slug:slug>/publish/", views.workflow_publish, name="publish"),
    path("<slug:slug>/archive/", views.workflow_archive, name="archive"),
    path("<slug:slug>/delete/", views.workflow_delete, name="delete"),
]
