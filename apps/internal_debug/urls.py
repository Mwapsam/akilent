from django.urls import path

from apps.internal_debug import views

urlpatterns = [
    path("whatsapp-numbers/", views.WhatsAppNumbersView.as_view(), name="internal-debug-whatsapp-numbers"),
    path("webhook-events/", views.WebhookEventsView.as_view(), name="internal-debug-webhook-events"),
    path("webhook-events/<int:event_id>/resend/", views.WebhookEventResendView.as_view(), name="internal-debug-webhook-event-resend"),
    path("onboarding-state/", views.OnboardingStateView.as_view(), name="internal-debug-onboarding-state"),
    path("page/", views.InternalPageView.as_view(), name="internal-debug-page"),
]
