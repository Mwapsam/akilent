from django.urls import path

from apps.crm import views

app_name = "crm"

urlpatterns = [
    path("", views.sales, name="sales"),
    path("leads/create/", views.create_lead_view, name="create-lead"),
    path("leads/<str:public_id>/", views.lead_detail, name="lead-detail"),
    path("deals/<str:public_id>/", views.deal_detail, name="deal-detail"),
]
