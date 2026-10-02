from django.urls import path

from apps.support.views import agent, customer

app_name = "support"

urlpatterns = [
    # Agent inbox
    path("", agent.inbox, name="inbox"),
    path("<str:ticket_number>/", agent.ticket_detail, name="detail"),
    # Customer portal
    path("portal/", customer.portal, name="portal"),
    path("portal/new/", customer.portal_create, name="portal-create"),
    path("portal/<str:ticket_number>/", customer.portal_ticket, name="portal-ticket"),
]
