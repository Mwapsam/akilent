from django.urls import path

from apps.scheduler import views

app_name = "scheduler"

urlpatterns = [
    path("", views.scheduled_index, name="index"),
    path("<str:public_id>/cancel/", views.scheduled_cancel, name="cancel"),
    path("<str:public_id>/reschedule/", views.scheduled_reschedule, name="reschedule"),
]
