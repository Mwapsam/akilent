from django.urls import path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.api import views

urlpatterns = [
    path("schema", SpectacularAPIView.as_view(), name="api-schema"),
    path(
        "docs",
        SpectacularSwaggerView.as_view(url_name="api-schema"),
        name="api-docs",
    ),
    path(
        "reference",
        SpectacularRedocView.as_view(url_name="api-schema"),
        name="api-reference",
    ),
    path("<str:version>/messages", views.MessageCreateView.as_view(), name="api-v1-messages"),
    path("<str:version>/messages/<str:public_id>", views.MessageDetailView.as_view(), name="api-v1-message-detail"),
    path("<str:version>/messages/<str:public_id>/events", views.MessageEventsView.as_view(), name="api-v1-message-events"),
    path("<str:version>/templates", views.TemplateListCreateView.as_view(), name="api-v1-templates"),
    path("<str:version>/templates/<slug:slug>", views.TemplateDetailView.as_view(), name="api-v1-template-detail"),
    path("<str:version>/templates/<slug:slug>/render", views.TemplateRenderView.as_view(), name="api-v1-template-render"),
    path("<str:version>/templates/<slug:slug>/preview", views.TemplatePreviewView.as_view(), name="api-v1-template-preview"),
    path("<str:version>/templates/<slug:slug>/clone", views.TemplateCloneView.as_view(), name="api-v1-template-clone"),
    path("<str:version>/campaigns", views.CampaignCreateView.as_view(), name="api-v1-campaigns"),
    path("<str:version>/campaigns/<int:pk>", views.CampaignDetailView.as_view(), name="api-v1-campaign-detail"),
    path("<str:version>/request-logs", views.RequestLogListView.as_view(), name="api-v1-request-logs"),
    path("<str:version>/request-logs/<str:request_id>", views.RequestLogDetailView.as_view(), name="api-v1-request-log-detail"),
]
