from django.urls import path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.api import analytics_views, contact_views, event_views, views, workflow_views

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
    path("<str:version>/templates/<slug:slug>/versions", views.TemplateVersionsView.as_view(), name="api-v1-template-versions"),
    path("<str:version>/templates/<slug:slug>/versions/<int:number>/activate", views.TemplateVersionActivateView.as_view(), name="api-v1-template-version-activate"),
    path("<str:version>/campaigns", views.CampaignCreateView.as_view(), name="api-v1-campaigns"),
    path("<str:version>/campaigns/<int:pk>", views.CampaignDetailView.as_view(), name="api-v1-campaign-detail"),
    path("<str:version>/request-logs", views.RequestLogListView.as_view(), name="api-v1-request-logs"),
    path("<str:version>/request-logs/<str:request_id>", views.RequestLogDetailView.as_view(), name="api-v1-request-log-detail"),
    path("<str:version>/deliverability", views.DeliverabilityView.as_view(), name="api-v1-deliverability"),
    path("<str:version>/analytics", analytics_views.AnalyticsView.as_view(), name="api-v1-analytics"),
    path("<str:version>/version", views.ApiVersionView.as_view(), name="api-v1-version"),
    path("<str:version>/changelog", views.ChangelogView.as_view(), name="api-v1-changelog"),

    # Contacts / lists / segments (Phase 4)
    path("<str:version>/contacts", contact_views.ContactCollectionView.as_view(), name="api-v1-contacts"),
    path("<str:version>/contacts/import", contact_views.ContactImportView.as_view(), name="api-v1-contacts-import"),
    path("<str:version>/contacts/<str:cid>", contact_views.ContactDetailView.as_view(), name="api-v1-contact-detail"),
    path("<str:version>/contacts/<str:cid>/events", contact_views.ContactEventsView.as_view(), name="api-v1-contact-events"),
    path("<str:version>/lists", contact_views.ListCollectionView.as_view(), name="api-v1-lists"),
    path("<str:version>/segments", contact_views.SegmentCollectionView.as_view(), name="api-v1-segments"),
    path("<str:version>/segments/preview", contact_views.SegmentPreviewView.as_view(), name="api-v1-segment-preview"),
    path("<str:version>/segments/<slug:slug>/contacts", contact_views.SegmentContactsView.as_view(), name="api-v1-segment-contacts"),

    # Workflows (Phase 6)
    path("<str:version>/workflows", workflow_views.WorkflowCollectionView.as_view(), name="api-v1-workflows"),
    path("<str:version>/workflows/templates", workflow_views.WorkflowTemplateCatalogView.as_view(), name="api-v1-workflow-templates"),
    path("<str:version>/workflows/<slug:slug>", workflow_views.WorkflowDetailView.as_view(), name="api-v1-workflow-detail"),
    path("<str:version>/workflows/<slug:slug>/publish", workflow_views.WorkflowPublishView.as_view(), name="api-v1-workflow-publish"),
    path("<str:version>/workflows/<slug:slug>/archive", workflow_views.WorkflowArchiveView.as_view(), name="api-v1-workflow-archive"),
    path("<str:version>/workflows/<slug:slug>/runs", workflow_views.WorkflowRunsView.as_view(), name="api-v1-workflow-runs"),
    path("<str:version>/workflow-runs/<str:run_id>", workflow_views.WorkflowRunDetailView.as_view(), name="api-v1-workflow-run-detail"),

    # Business events (Phase 5)
    path("<str:version>/events", event_views.EventCollectionView.as_view(), name="api-v1-events"),
    path("<str:version>/events/catalog", event_views.EventCatalogView.as_view(), name="api-v1-events-catalog"),
    path("<str:version>/webhooks/test", event_views.WebhookTestView.as_view(), name="api-v1-webhooks-test"),
]
