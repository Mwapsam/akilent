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
    # Admin package management
    path("plans/create/", views.plan_create, name="plan-create"),
    path("plans/<int:pk>/edit/", views.plan_edit, name="plan-edit"),
    path("plans/<int:pk>/toggle/", views.plan_toggle, name="plan-toggle"),
    path("plans/<int:pk>/delete/", views.plan_delete, name="plan-delete"),
    path("plans/<int:pk>/sync-fw/", views.plan_sync_fw, name="plan-sync-fw"),
    # Manual (offline) payments
    path("manual/submit/", views.manual_submit, name="manual-submit"),
    path("manual/<int:pk>/approve/", views.manual_approve, name="manual-approve"),
    path("manual/<int:pk>/reject/", views.manual_reject, name="manual-reject"),
    # Admin: payment method management
    path("methods/<int:pk>/toggle/", views.payment_method_toggle, name="payment-method-toggle"),
    path("methods/<int:pk>/edit/", views.payment_method_edit, name="payment-method-edit"),
]
