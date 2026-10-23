from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    path("plans/", views.pricing_page, name="plans"),
    path("locked/<slug:key>/", views.feature_locked, name="locked"),
    path("checkout/", views.checkout, name="checkout"),
    # Every payment provider returns and posts webhooks to the same two routes.
    path("return/<slug:provider>/", views.payment_return, name="return"),
    path("webhooks/<slug:provider>/", views.provider_webhook, name="webhook-provider"),
    path(
        "payments/<str:reference>/status/", views.payment_status, name="payment-status"
    ),
    path("cancel/", views.cancel_subscription, name="cancel"),
    path("resume/", views.resume_subscription, name="resume"),
    # Old provider-specific URLs, still set in the Flutterwave and Stripe dashboards.
    path("callback/", views.callback, name="callback"),
    path("webhook/", views.webhook, name="webhook"),
    path("stripe/success/", views.stripe_success, name="stripe-success"),
    path("stripe/webhook/", views.stripe_webhook, name="stripe-webhook"),
    # Manual (offline) payments: the business submits here; operators approve in the console.
    path("manual/submit/", views.manual_submit, name="manual-submit"),
    # Package, payment-method and approval actions are routed under /manage/ (apps/core/urls.py).
]
