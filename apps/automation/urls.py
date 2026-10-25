from django.urls import path

from apps.automation import views

app_name = "automation"

urlpatterns = [
    path("", views.workflow_list, name="list"),
    path("create/", views.workflow_create, name="create"),
    # Before the <slug> route, which would otherwise swallow it.
    path("starters/install/", views.starter_install, name="starter-install"),
    path("why-not/", views.why_not, name="why-not"),
    path("settings/", views.automation_settings, name="settings"),
    path("starters/test/", views.starter_test, name="starter-test"),
    path("runs/<str:run_id>/", views.run_detail, name="run"),
    path("runs/<str:run_id>/<str:action>/", views.run_action, name="run-action"),
    path("<slug:slug>/", views.workflow_editor, name="editor"),
    path("<slug:slug>/stats/", views.workflow_stats, name="stats"),
    path("<slug:slug>/runs/", views.workflow_runs, name="runs"),
    path("<slug:slug>/save/", views.workflow_save, name="save"),
    path("<slug:slug>/validate/", views.workflow_validate, name="validate"),
    path("<slug:slug>/simulate/", views.workflow_simulate, name="simulate"),
    path("<slug:slug>/coverage/", views.workflow_coverage, name="coverage"),
    path("<slug:slug>/enroll/", views.workflow_enroll, name="enroll"),
    path("<slug:slug>/publish/", views.workflow_publish, name="publish"),
    path("<slug:slug>/discard/", views.workflow_discard, name="discard"),
    path("<slug:slug>/archive/", views.workflow_archive, name="archive"),
    path("<slug:slug>/pause/", views.workflow_pause, name="pause"),
    path("<slug:slug>/resume/", views.workflow_resume, name="resume"),
    path("<slug:slug>/delete/", views.workflow_delete, name="delete"),
]
