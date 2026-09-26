from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("plans/", views.pricing_page, name="plans"),
    path("checkout/", views.checkout, name="checkout"),
    path("callback/", views.callback, name="callback"),
    path("webhook/", views.webhook, name="webhook"),
    path("cancel/", views.cancel_subscription, name="cancel"),
    path("stripe/success/", views.stripe_success, name="stripe-success"),
    path("stripe/webhook/", views.stripe_webhook, name="stripe-webhook"),
    # Manual (offline) payments: the business submits here; operators approve in the console.
    path("manual/submit/", views.manual_submit, name="manual-submit"),
    # Package, payment-method and approval actions are routed under /manage/ (apps/core/urls.py).
]
