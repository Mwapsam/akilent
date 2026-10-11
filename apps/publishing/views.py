"""Publishing views: Facebook OAuth, ContentPost CRUD, AI caption generation."""

from __future__ import annotations

import logging
import secrets

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account

from .models import ContentCaption, ContentPost, FacebookPageConnection
from .providers.facebook import FacebookPublishError, list_pages

logger = logging.getLogger(__name__)

_FB_GRAPH = "https://graph.facebook.com"
_FB_SCOPES = "pages_manage_posts,pages_read_engagement"


# ---------------------------------------------------------------------------
# Facebook OAuth
# ---------------------------------------------------------------------------


@login_required
def fb_oauth_connect(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    app_id = getattr(settings, "FACEBOOK_APP_ID", "") or getattr(
        settings, "INSTAGRAM_APP_ID", ""
    )
    if not app_id:
        messages.error(request, "Facebook App ID is not configured.")
        return redirect("settings-integrations")

    state = secrets.token_urlsafe(32)
    conn, _ = FacebookPageConnection.objects.get_or_create(account=account)
    conn.oauth_state = state
    conn.oauth_state_created_at = timezone.now()
    conn.save(update_fields=["oauth_state", "oauth_state_created_at"])

    redirect_uri = request.build_absolute_uri("/publishing/fb/callback/")
    request.session["fb_connect_redirect_uri"] = redirect_uri

    auth_url = (
        f"https://www.facebook.com/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={_FB_SCOPES}"
        f"&state={state}"
    )
    return redirect(auth_url)


@login_required
def fb_oauth_callback(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    error = request.GET.get("error")
    if error:
        messages.error(
            request,
            f"Facebook connection failed: {request.GET.get('error_description', error)}",
        )
        return redirect("settings-integrations")

    code = request.GET.get("code")
    state = request.GET.get("state")
    redirect_uri = request.session.pop("fb_connect_redirect_uri", None)

    if not code or not redirect_uri:
        messages.error(request, "Invalid Facebook callback.")
        return redirect("settings-integrations")

    try:
        conn = account.facebook_page_connection
    except FacebookPageConnection.DoesNotExist:
        messages.error(request, "No pending Facebook connection found.")
        return redirect("settings-integrations")

    if conn.oauth_state != state:
        messages.error(request, "Invalid state parameter.")
        return redirect("settings-integrations")

    app_id = getattr(settings, "FACEBOOK_APP_ID", "") or getattr(
        settings, "INSTAGRAM_APP_ID", ""
    )
    app_secret = getattr(settings, "FACEBOOK_APP_SECRET", "") or getattr(
        settings, "INSTAGRAM_APP_SECRET", ""
    )

    # Exchange code for short-lived user token
    r = requests.get(
        f"{_FB_GRAPH}/oauth/access_token",
        params={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    token_data = r.json()
    if "error" in token_data:
        messages.error(
            request,
            f"Facebook token exchange failed: {token_data['error'].get('message', '')}",
        )
        return redirect("settings-integrations")

    user_token = token_data["access_token"]

    # Fetch pages this user manages
    try:
        pages = list_pages(user_token)
    except FacebookPublishError as exc:
        messages.error(request, f"Could not fetch Facebook Pages: {exc}")
        return redirect("settings-integrations")

    if not pages:
        messages.error(request, "No Facebook Pages found for this account.")
        return redirect("settings-integrations")

    # Use the first page (or let user choose — simplified to first page for now)
    page = pages[0]
    conn.page_id = page["id"]
    conn.page_name = page.get("name", "")
    conn.access_token = page["access_token"]
    conn.oauth_state = ""
    conn.status = FacebookPageConnection.Status.ACTIVE
    conn.save()

    messages.success(request, f"Connected to Facebook Page: {conn.page_name}")
    return redirect("settings-integrations")


@login_required
@require_POST
def fb_oauth_disconnect(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    try:
        conn = account.facebook_page_connection
        conn.status = FacebookPageConnection.Status.DISCONNECTED
        conn.access_token = None
        conn.save(update_fields=["status", "access_token"])
    except FacebookPageConnection.DoesNotExist:
        pass

    messages.success(request, "Facebook Page disconnected.")
    return redirect("settings-integrations")


# ---------------------------------------------------------------------------
# Content calendar
# ---------------------------------------------------------------------------


@login_required
def publishing_index(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    posts = ContentPost.objects.filter(account=account).order_by(
        "-scheduled_at", "-created_at"
    )[:50]
    return render(
        request, "publishing/index.html", {"account": account, "posts": posts}
    )


@login_required
def post_new(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        caption = request.POST.get("caption", "").strip()
        hashtags_raw = request.POST.get("hashtags", "").strip()
        hashtags = [t.lstrip("#").strip() for t in hashtags_raw.split() if t.strip()]
        platforms = request.POST.getlist("platforms")
        media_type = request.POST.get("media_type", ContentPost.MediaType.IMAGE)
        media_urls_raw = request.POST.get("media_urls", "").strip()
        media_urls = [u.strip() for u in media_urls_raw.splitlines() if u.strip()]
        scheduled_at_raw = request.POST.get("scheduled_at", "").strip()

        scheduled_at = None
        if scheduled_at_raw:
            from django.utils.dateparse import parse_datetime

            scheduled_at = parse_datetime(scheduled_at_raw)

        status = (
            ContentPost.Status.SCHEDULED if scheduled_at else ContentPost.Status.DRAFT
        )

        post = ContentPost.objects.create(
            account=account,
            platforms=platforms,
            media_type=media_type,
            caption=caption,
            hashtags=hashtags,
            media_urls=media_urls,
            scheduled_at=scheduled_at,
            status=status,
            created_by=request.user,
        )
        messages.success(request, "Post saved.")
        return redirect("publishing:post-detail", pk=post.pk)

    return render(
        request,
        "publishing/post_new.html",
        {
            "account": account,
            "media_types": ContentPost.MediaType.choices,
        },
    )


@login_required
def post_detail(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)
    return render(
        request, "publishing/post_detail.html", {"account": account, "post": post}
    )


@login_required
@require_POST
def post_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)
    if post.status not in (ContentPost.Status.DRAFT, ContentPost.Status.SCHEDULED):
        messages.error(request, "Only draft or scheduled posts can be deleted.")
        return redirect("publishing:post-detail", pk=post.pk)

    post.delete()
    messages.success(request, "Post deleted.")
    return redirect("publishing:index")


# ---------------------------------------------------------------------------
# AI caption generation (HTMX endpoint)
# ---------------------------------------------------------------------------


@login_required
@require_POST
def generate_caption(request):
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "No account"}, status=403)

    prompt = request.POST.get("prompt", "").strip()
    media_type = request.POST.get("media_type", "image")
    platform = request.POST.get("platform", "instagram")
    post_id = request.POST.get("post_id", "")

    if not prompt:
        return JsonResponse({"error": "Prompt is required."}, status=400)

    from apps.ai import tasks as ai_tasks

    draft_id = ai_tasks.build_caption_draft(account.id, prompt, media_type, platform)

    if post_id:
        try:
            post = ContentPost.objects.get(pk=post_id, account=account)
            ContentCaption.objects.create(
                post=post,
                prompt=prompt,
                body="",
                hashtags=[],
            )
        except ContentPost.DoesNotExist:
            pass

    return JsonResponse({"draft_id": draft_id})


@login_required
def caption_draft_result(request, draft_id):
    """Poll for caption draft completion."""
    from apps.ai.models import AIDraft

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "No account"}, status=403)

    try:
        draft = AIDraft.objects.get(pk=draft_id, account=account)
    except (AIDraft.DoesNotExist, ValueError):
        return JsonResponse({"error": "Not found"}, status=404)

    if draft.status == AIDraft.Status.PENDING:
        return JsonResponse({"status": "pending"})

    if draft.status == AIDraft.Status.ERROR:
        return JsonResponse(
            {"status": "failed", "error": draft.error or "Generation failed."}
        )

    result = draft.result or {}
    return JsonResponse(
        {
            "status": "ready",
            "caption": result.get("caption", ""),
            "hashtags": result.get("hashtags", []),
        }
    )
