from django.urls import path

from . import views

app_name = "publishing"

urlpatterns = [
    path("", views.publishing_index, name="index"),
    path("new/", views.post_new, name="post-new"),
    path("posts/<int:pk>/", views.post_detail, name="post-detail"),
    path("posts/<int:pk>/delete/", views.post_delete, name="post-delete"),
    path("caption/generate/", views.generate_caption, name="caption-generate"),
    path(
        "caption/result/<str:draft_id>/",
        views.caption_draft_result,
        name="caption-result",
    ),
    path("fb/connect/", views.fb_oauth_connect, name="fb-connect"),
    path("fb/callback/", views.fb_oauth_callback, name="fb-callback"),
    path("fb/disconnect/", views.fb_oauth_disconnect, name="fb-disconnect"),
]
