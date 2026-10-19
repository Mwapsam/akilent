from django.urls import path

from . import views

app_name = "publishing"

urlpatterns = [
    # Overview
    path("", views.publishing_index, name="index"),
    # Planning
    path("planning/", views.planning_overview, name="planning"),
    path("planning/new/", views.plan_item_new, name="plan-new"),
    path("planning/<int:pk>/edit/", views.plan_item_edit, name="plan-edit"),
    path("planning/<int:pk>/delete/", views.plan_item_delete, name="plan-delete"),
    path(
        "planning/<int:pk>/create-post/",
        views.plan_item_create_post,
        name="plan-create-post",
    ),
    path("pillars/", views.pillar_manage, name="pillars"),
    path("campaigns/", views.campaign_manage, name="campaigns"),
    # Calendar
    path("calendar/", views.publishing_calendar, name="calendar"),
    # Library
    path("library/", views.publishing_library, name="library"),
    path("library/bulk/", views.publishing_library_bulk, name="library-bulk"),
    # Channels
    path("channels/", views.publishing_channels, name="channels"),
    path("settings/", views.publishing_settings_update, name="settings"),
    # Posts
    path("posts/media/upload/", views.post_media_upload, name="post-media-upload"),
    path("new/", views.post_new, name="post-new"),
    path("posts/<int:pk>/", views.post_detail, name="post-detail"),
    path("posts/<int:pk>/delete/", views.post_delete, name="post-delete"),
    path("posts/<int:pk>/retry/", views.post_retry, name="post-retry"),
    path(
        "posts/<int:pk>/retry/<str:platform>/",
        views.post_retry_variant,
        name="post-retry-variant",
    ),
    path(
        "posts/<int:pk>/channels/<str:platform>/reschedule/",
        views.variant_reschedule,
        name="variant-reschedule",
    ),
    path("posts/<int:pk>/cancel/", views.post_cancel, name="post-cancel"),
    path(
        "posts/<int:pk>/variants/<int:variant_pk>/first-comment/retry/",
        views.first_comment_retry,
        name="first-comment-retry",
    ),
    path("locations/search/", views.location_search, name="location-search"),
    path("posts/<int:pk>/reopen/", views.post_reopen, name="post-reopen"),
    path(
        "posts/<int:pk>/refresh-insights/",
        views.post_refresh_insights,
        name="post-refresh-insights",
    ),
    # Approval workflow
    path(
        "posts/<int:pk>/submit-for-review/",
        views.post_submit_for_review,
        name="post-submit-for-review",
    ),
    path(
        "posts/<int:pk>/approve/",
        views.post_approve,
        name="post-approve",
    ),
    path(
        "posts/<int:pk>/request-changes/",
        views.post_request_changes,
        name="post-request-changes",
    ),
    path(
        "posts/<int:pk>/resubmit-after-changes/",
        views.post_resubmit_after_changes,
        name="post-resubmit-after-changes",
    ),
    # AI caption generation
    path("caption/generate/", views.generate_caption, name="caption-generate"),
    path(
        "caption/result/<str:draft_id>/",
        views.caption_draft_result,
        name="caption-result",
    ),
    # Reconciliation (Step 4.7B)
    path("reconciliation/", views.reconciliation_queue, name="reconciliation-queue"),
    path(
        "reconciliation/<int:variant_id>/confirm-published/",
        views.reconciliation_confirm_published,
        name="reconciliation-confirm",
    ),
    path(
        "reconciliation/<int:variant_id>/retry/",
        views.reconciliation_retry,
        name="reconciliation-retry",
    ),
    path(
        "reconciliation/<int:variant_id>/leave-unresolved/",
        views.reconciliation_leave_unresolved,
        name="reconciliation-unresolved",
    ),
    # Facebook OAuth
    path("fb/connect/", views.fb_oauth_connect, name="fb-connect"),
    path("fb/callback/", views.fb_oauth_callback, name="fb-callback"),
    path("fb/select-page/", views.fb_select_page, name="fb-select-page"),
    path("fb/disconnect/", views.fb_oauth_disconnect, name="fb-disconnect"),
]
