from django.urls import path

from apps.commerce import views

app_name = "commerce"

urlpatterns = [
    path("", views.orders, name="orders"),
    path("<str:public_id>/", views.order_detail, name="detail"),
]
