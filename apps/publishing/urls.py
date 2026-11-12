from django.urls import path

from . import (
    ai_views,
    analytics_views,
    bio_views,
    comment_reply_views,
    import_views,
    media_views,
    recurring_views,
    sales_views,
    scheduling_views,
    views,
)

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
    # Scheduling (release gate cs_scheduling)
    path("calendar/move/", scheduling_views.calendar_move, name="calendar-move"),
    path("calendar/move-plan/", scheduling_views.plan_move, name="plan-move"),
    path("slots/", scheduling_views.slots_manage, name="slots"),
    path("slots/next/", scheduling_views.slot_next, name="slot-next"),
    # Library
    path("library/", views.publishing_library, name="library"),
    path("library/bulk/", views.publishing_library_bulk, name="library-bulk"),
    # Channels
    path("channels/", views.publishing_channels, name="channels"),
    path("settings/", views.publishing_settings_update, name="settings"),
    # Posts
    path("posts/media/upload/", views.post_media_upload, name="post-media-upload"),
    path(
        "posts/media/direct/start/",
        views.post_media_direct_start,
        name="post-media-direct-start",
    ),
    path(
        "posts/media/direct/confirm/",
        views.post_media_direct_confirm,
        name="post-media-direct-confirm",
    ),
    path("new/", views.post_new, name="post-new"),
    path("new/readiness/", views.composer_readiness, name="composer-readiness"),
    path("new/autosave/", views.composer_autosave, name="composer-autosave"),
    path("posts/<int:pk>/", views.post_detail, name="post-detail"),
    path(
        "posts/<int:pk>/comment-replies/",
        comment_reply_views.comment_reply_save,
        name="comment-reply-add",
    ),
    path(
        "posts/<int:pk>/comment-replies/<int:rule_pk>/",
        comment_reply_views.comment_reply_save,
        name="comment-reply-edit",
    ),
    path(
        "posts/<int:pk>/comment-replies/<int:rule_pk>/toggle/",
        comment_reply_views.comment_reply_toggle,
        name="comment-reply-toggle",
    ),
    path(
        "posts/<int:pk>/comment-replies/<int:rule_pk>/delete/",
        comment_reply_views.comment_reply_delete,
        name="comment-reply-delete",
    ),
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
    path("posts/undo-cancel/", views.post_undo_cancel, name="post-undo-cancel"),
    path(
        "posts/<int:pk>/variants/<int:variant_pk>/first-comment/retry/",
        views.first_comment_retry,
        name="first-comment-retry",
    ),
    path("locations/search/", views.location_search, name="location-search"),
    path("posts/<int:pk>/reopen/", views.post_reopen, name="post-reopen"),
    path("posts/<int:pk>/fix/", views.post_fix, name="post-fix"),
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
    # Account analytics (release gate cs_analytics)
    path("analytics/", analytics_views.analytics, name="analytics"),
    path("analytics/print/", analytics_views.analytics_print, name="analytics-print"),
    path("analytics/export.csv", analytics_views.analytics_csv, name="analytics-csv"),
    path(
        "analytics/weekly/", analytics_views.analytics_weekly, name="analytics-weekly"
    ),
    # Recurring posts (release gate cs_recurring)
    path("recurring/", recurring_views.recurring_list, name="recurring"),
    path(
        "recurring/<int:pk>/",
        recurring_views.recurring_update,
        name="recurring-update",
    ),
    path(
        "recurring/new/<int:pk>/",
        recurring_views.recurring_create,
        name="recurring-create",
    ),
    # Media library, editor, templates and hashtag sets (release gate cs_media_library)
    # Content Studio AI tools (cs_ai_plus).
    path("ai/", ai_views.ai_tools, name="ai-tools"),
    path("ai/rewrite/", ai_views.rewrite, name="ai-rewrite"),
    path("ai/hashtags/", ai_views.hashtags, name="ai-hashtags"),
    path("ai/result/<int:draft_id>/", ai_views.result, name="ai-result"),
    path("ai/ideas/", ai_views.ideas, name="ai-ideas"),
    path("ai/ideas/<int:draft_id>/", ai_views.ideas_status, name="ai-ideas-status"),
    path("ai/ideas/<int:draft_id>/save/", ai_views.idea_save, name="ai-idea-save"),
    path("media/", media_views.library, name="media-library"),
    path("media/<int:pk>/", media_views.asset_update, name="media-update"),
    path("media/<int:pk>/", media_views.asset_update, name="media-detail"),
    path("media/bulk/", media_views.bulk, name="media-bulk"),
    path("media/<int:pk>/restore/", media_views.asset_restore, name="media-restore"),
    path("media/restore/", media_views.restore_many, name="media-restore-many"),
    path("media/<int:pk>/edit/", media_views.asset_edit, name="media-edit"),
    path("media/<int:pk>/file/", media_views.asset_file, name="media-file"),
    path("media/<int:pk>/cover/", media_views.asset_cover, name="media-cover"),
    path("media/picker.json", media_views.picker, name="media-picker"),
    path("media/similar.json", media_views.similar, name="media-similar"),
    path(
        "media/stock/search.json", media_views.stock_search, name="media-stock-search"
    ),
    path("media/stock/import/", media_views.stock_import, name="media-stock-import"),
    path("media/snippets/", media_views.snippets, name="media-snippets"),
    # Bulk import (release gate cs_csv_import)
    path("import/", import_views.csv_import_page, name="csv-import"),
    # Which posts sold (release gate cs_sales_report)
    path("sales/", sales_views.sales_report, name="sales"),
    # Link-in-bio page (release gate cs_bio_page)
    path("bio/", bio_views.bio_manage, name="bio"),
    path(
        "bio/tiles/add/<int:variant_pk>/",
        bio_views.bio_tile_add,
        name="bio-tile-add",
    ),
    path("bio/tiles/<int:pk>/", bio_views.bio_tile_update, name="bio-tile-update"),
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
    path("ig/disconnect/", views.ig_disconnect, name="ig-disconnect"),
]
