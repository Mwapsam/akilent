from django.urls import path

from apps.commerce import views

app_name = "commerce"

urlpatterns = [
    path("", views.orders, name="orders"),
    path("create/", views.create_order_view, name="create-order"),
    path("<str:public_id>/", views.order_detail, name="detail"),
]
