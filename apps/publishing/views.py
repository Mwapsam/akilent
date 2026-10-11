"""Publishing views: Facebook OAuth, ContentPost CRUD, planning, AI caption generation."""

from __future__ import annotations

import datetime
import logging
import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.core.htmx import full_page_load

from .models import (
    ContentCampaign,
    ContentCaption,
    ContentPillar,
    ContentPlanItem,
    ContentPost,
    FacebookPageConnection,
)
from .providers.facebook import FacebookPublishError, list_pages

logger = logging.getLogger(__name__)

_FB_GRAPH = "https://graph.facebook.com"
_FB_SCOPES = "pages_manage_posts,pages_read_engagement"

# Valid status transitions: {from_status: allowed_to_statuses}
_TRANSITIONS = {
    ContentPost.Status.IDEA: [ContentPost.Status.DRAFT],
    ContentPost.Status.DRAFT: [
        ContentPost.Status.IN_REVIEW,
        ContentPost.Status.SCHEDULED,
        ContentPost.Status.PUBLISHING,
    ],
    ContentPost.Status.IN_REVIEW: [
        ContentPost.Status.APPROVED,
        ContentPost.Status.DRAFT,
    ],
    ContentPost.Status.APPROVED: [
        ContentPost.Status.SCHEDULED,
        ContentPost.Status.PUBLISHING,
    ],
    ContentPost.Status.SCHEDULED: [ContentPost.Status.CANCELLED],
    ContentPost.Status.FAILED: [ContentPost.Status.SCHEDULED],
    ContentPost.Status.CANCELLED: [ContentPost.Status.DRAFT],
}


# ---------------------------------------------------------------------------
# Facebook OAuth
# ---------------------------------------------------------------------------


@full_page_load
@login_required
def fb_oauth_connect(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    app_id = getattr(settings, "WHATSAPP_APP_ID", "")
    if not app_id:
        messages.error(request, "Facebook App ID is not configured.")
        return redirect("settings-integrations")

    state = secrets.token_urlsafe(32)
    conn, _ = FacebookPageConnection.objects.get_or_create(account=account)
    conn.oauth_state = state
    conn.oauth_state_created_at = timezone.now()
    conn.save(update_fields=["oauth_state", "oauth_state_created_at"])

    redirect_uri = request.build_absolute_uri("/publishing/fb/callback/")

    graph_version = getattr(settings, "WHATSAPP_GRAPH_VERSION", "v21.0")
    auth_url = (
        f"https://www.facebook.com/{graph_version}/dialog/oauth"
        f"?{urlencode({'client_id': app_id, 'redirect_uri': redirect_uri, 'scope': _FB_SCOPES, 'state': state})}"
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
    request.session.pop("fb_connect_redirect_uri", None)
    redirect_uri = request.build_absolute_uri("/publishing/fb/callback/")

    if not code:
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

    app_id = getattr(settings, "WHATSAPP_APP_ID", "")
    app_secret = getattr(settings, "WHATSAPP_APP_SECRET", "")

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

    try:
        pages = list_pages(user_token)
    except FacebookPublishError as exc:
        messages.error(request, f"Could not fetch Facebook Pages: {exc}")
        return redirect("settings-integrations")

    if not pages:
        messages.error(request, "No Facebook Pages found for this account.")
        return redirect("settings-integrations")

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
# Content Studio overview
# ---------------------------------------------------------------------------


@login_required
def publishing_index(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    month_start = today.replace(day=1)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    month_start_dt = timezone.make_aware(
        datetime.datetime.combine(month_start, datetime.time.min), tz
    )
    next_month_dt = timezone.make_aware(
        datetime.datetime.combine(next_month, datetime.time.min), tz
    )

    qs = ContentPost.objects.filter(account=account)
    counts = {
        "in_progress": qs.filter(
            status__in=[
                ContentPost.Status.IDEA,
                ContentPost.Status.DRAFT,
                ContentPost.Status.IN_REVIEW,
                ContentPost.Status.APPROVED,
            ]
        ).count(),
        "scheduled": qs.filter(status=ContentPost.Status.SCHEDULED).count(),
        "published_this_month": qs.filter(
            status=ContentPost.Status.PUBLISHED,
            published_at__gte=month_start_dt,
            published_at__lt=next_month_dt,
        ).count(),
        "needs_attention": qs.filter(status=ContentPost.Status.FAILED).count(),
    }
    upcoming = list(
        qs.filter(status=ContentPost.Status.SCHEDULED).order_by("scheduled_at")[:5]
    )
    failed_recent = list(
        qs.filter(status=ContentPost.Status.FAILED).order_by("-updated_at")[:3]
    )

    return render(
        request,
        "publishing/index.html",
        {
            "account": account,
            "counts": counts,
            "upcoming": upcoming,
            "failed_recent": failed_recent,
        },
    )


# ---------------------------------------------------------------------------
# Content Calendar
# ---------------------------------------------------------------------------


@login_required
def publishing_calendar(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    tz = timezone.get_current_timezone()
    today = timezone.localdate()

    # Parse ?week=YYYY-Www
    week_param = request.GET.get("week", "")
    try:
        year_str, week_str = week_param.split("-W")
        year, week_num = int(year_str), int(week_str)
        week_start = datetime.date.fromisocalendar(year, week_num, 1)
    except (ValueError, AttributeError):
        # Default to current week
        iso = today.isocalendar()
        week_start = datetime.date.fromisocalendar(iso[0], iso[1], 1)

    week_end = week_start + datetime.timedelta(days=6)

    # Build aware datetime boundaries
    week_start_dt = timezone.make_aware(
        datetime.datetime.combine(week_start, datetime.time.min), tz
    )
    week_end_dt = timezone.make_aware(
        datetime.datetime.combine(week_end, datetime.time.max), tz
    )

    # Navigation
    prev_week_start = week_start - datetime.timedelta(days=7)
    next_week_start = week_start + datetime.timedelta(days=7)
    prev_iso = prev_week_start.isocalendar()
    next_iso = next_week_start.isocalendar()
    prev_week_param = f"{prev_iso[0]}-W{prev_iso[1]:02d}"
    next_week_param = f"{next_iso[0]}-W{next_iso[1]:02d}"

    # Server-side filters
    channel_filter = request.GET.get("channel", "")
    status_filter = request.GET.get("status", "")
    view_mode = request.GET.get("view", "week")

    posts_base = ContentPost.objects.filter(
        account=account,
        scheduled_at__gte=week_start_dt,
        scheduled_at__lte=week_end_dt,
    ).order_by("scheduled_at")

    posts_qs: list[ContentPost] = list(posts_base)
    if channel_filter:
        posts_qs = [p for p in posts_qs if channel_filter in (p.platforms or [])]
    if status_filter:
        posts_qs = [p for p in posts_qs if p.status == status_filter]

    plan_items_qs = ContentPlanItem.objects.filter(
        account=account,
        planned_date__gte=week_start,
        planned_date__lte=week_end,
    ).order_by("planned_date")

    # Build week_days: list of (date, plan_items, posts)
    week_days = []
    for i in range(7):
        day = week_start + datetime.timedelta(days=i)
        day_posts = [
            p for p in posts_qs if timezone.localtime(p.scheduled_at, tz).date() == day
        ]
        day_plan_items = [pi for pi in plan_items_qs if pi.planned_date == day]
        week_days.append((day, day_plan_items, day_posts))

    unscheduled_drafts = list(
        ContentPost.objects.filter(
            account=account,
            scheduled_at__isnull=True,
            status__in=[
                ContentPost.Status.DRAFT,
                ContentPost.Status.APPROVED,
                ContentPost.Status.IDEA,
            ],
        ).order_by("-created_at")[:10]
    )

    return render(
        request,
        "publishing/calendar.html",
        {
            "account": account,
            "week_days": week_days,
            "week_start": week_start,
            "week_end": week_end,
            "prev_week_param": prev_week_param,
            "next_week_param": next_week_param,
            "view_mode": view_mode,
            "channel_filter": channel_filter,
            "status_filter": status_filter,
            "unscheduled_drafts": unscheduled_drafts,
            "status_choices": ContentPost.Status.choices,
        },
    )


# ---------------------------------------------------------------------------
# Content Library
# ---------------------------------------------------------------------------


@login_required
def publishing_library(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    qs = ContentPost.objects.filter(account=account).order_by("-created_at")

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(caption__icontains=q)

    status_filter = request.GET.get("status", "")
    if status_filter:
        qs = qs.filter(status=status_filter)

    media_type_filter = request.GET.get("media_type", "")
    if media_type_filter:
        qs = qs.filter(media_type=media_type_filter)

    date_from = request.GET.get("date_from", "")
    if date_from:
        try:
            qs = qs.filter(created_at__date__gte=datetime.date.fromisoformat(date_from))
        except ValueError:
            pass

    date_to = request.GET.get("date_to", "")
    if date_to:
        try:
            qs = qs.filter(created_at__date__lte=datetime.date.fromisoformat(date_to))
        except ValueError:
            pass

    platform_filter = request.GET.get("platform", "")
    posts = list(qs)
    if platform_filter:
        posts = [p for p in posts if platform_filter in (p.platforms or [])]

    from django.core.paginator import Paginator

    paginator = Paginator(posts, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    return render(
        request,
        "publishing/library.html",
        {
            "account": account,
            "page_obj": page_obj,
            "q": q,
            "status_filter": status_filter,
            "media_type_filter": media_type_filter,
            "platform_filter": platform_filter,
            "date_from": date_from,
            "date_to": date_to,
            "status_choices": ContentPost.Status.choices,
            "media_type_choices": ContentPost.MediaType.choices,
        },
    )


@login_required
@require_POST
def publishing_library_bulk(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    action = request.POST.get("action", "")
    raw_ids = request.POST.getlist("post_ids")

    try:
        pks = [int(pk) for pk in raw_ids]
    except (ValueError, TypeError):
        messages.error(request, "Invalid selection.")
        return redirect("publishing:library")

    if not pks:
        messages.error(request, "No posts selected.")
        return redirect("publishing:library")

    # Validate all PKs belong to this account before any mutation
    owned_count = ContentPost.objects.filter(pk__in=pks, account=account).count()
    if owned_count != len(pks):
        messages.error(request, "Invalid selection.")
        return redirect("publishing:library")

    if action == "delete_drafts":
        deleted, _ = ContentPost.objects.filter(
            pk__in=pks,
            account=account,
            status__in=[ContentPost.Status.IDEA, ContentPost.Status.DRAFT],
        ).delete()
        messages.success(request, f"Deleted {deleted} draft(s).")

    elif action == "cancel_scheduled":
        updated = ContentPost.objects.filter(
            pk__in=pks,
            account=account,
            status=ContentPost.Status.SCHEDULED,
        ).update(status=ContentPost.Status.CANCELLED)
        messages.success(request, f"Cancelled {updated} scheduled post(s).")

    else:
        messages.error(request, "Unknown action.")

    return redirect("publishing:library")


# ---------------------------------------------------------------------------
# Connected Channels
# ---------------------------------------------------------------------------


@login_required
def publishing_channels(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    ig = account.instagram_accounts.filter(is_active=True).first()
    ig_publish_scope = bool(ig and ig.access_token)

    try:
        fb_connection = account.facebook_page_connection
    except FacebookPageConnection.DoesNotExist:
        fb_connection = None

    recent_failures = list(
        ContentPost.objects.filter(
            account=account, status=ContentPost.Status.FAILED
        ).order_by("-updated_at")[:5]
    )

    return render(
        request,
        "publishing/channels.html",
        {
            "account": account,
            "ig_connection": ig,
            "ig_publish_scope": ig_publish_scope,
            "fb_connection": fb_connection,
            "recent_failures": recent_failures,
        },
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@login_required
def planning_overview(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    today = timezone.localdate()
    week_ahead = today + datetime.timedelta(days=7)

    plan_qs = ContentPlanItem.objects.filter(account=account)
    counts = {
        "idea": plan_qs.filter(plan_status=ContentPlanItem.PlanStatus.IDEA).count(),
        "planned": plan_qs.filter(
            plan_status=ContentPlanItem.PlanStatus.PLANNED
        ).count(),
        "in_production": plan_qs.filter(
            plan_status=ContentPlanItem.PlanStatus.IN_PRODUCTION
        ).count(),
        "published": plan_qs.filter(
            plan_status=ContentPlanItem.PlanStatus.PUBLISHED
        ).count(),
    }

    upcoming = list(
        plan_qs.filter(
            planned_date__gte=today,
            planned_date__lte=week_ahead,
        ).order_by("planned_date")
    )

    # Ideas backlog: no planned date
    backlog = list(
        plan_qs.filter(planned_date__isnull=True)
        .exclude(plan_status=ContentPlanItem.PlanStatus.PUBLISHED)
        .order_by("-created_at")[:15]
    )

    pillars = list(ContentPillar.objects.filter(account=account))
    campaigns = list(ContentCampaign.objects.filter(account=account))

    return render(
        request,
        "publishing/planning.html",
        {
            "account": account,
            "counts": counts,
            "upcoming": upcoming,
            "backlog": backlog,
            "pillars": pillars,
            "campaigns": campaigns,
        },
    )


@login_required
def plan_item_new(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        item = _save_plan_item(request, account, instance=None)
        if item:
            messages.success(request, "Idea added to your planning backlog.")
            return redirect("publishing:planning")

    pillars = ContentPillar.objects.filter(account=account)
    campaigns = ContentCampaign.objects.filter(account=account)
    return render(
        request,
        "publishing/plan_item_form.html",
        {
            "account": account,
            "pillars": pillars,
            "campaigns": campaigns,
            "media_type_choices": ContentPost.MediaType.choices,
            "plan_status_choices": ContentPlanItem.PlanStatus.choices,
            "item": None,
        },
    )


@login_required
def plan_item_edit(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    item = get_object_or_404(ContentPlanItem, pk=pk, account=account)

    if request.method == "POST":
        updated = _save_plan_item(request, account, instance=item)
        if updated:
            messages.success(request, "Plan item updated.")
            return redirect("publishing:planning")

    pillars = ContentPillar.objects.filter(account=account)
    campaigns = ContentCampaign.objects.filter(account=account)
    return render(
        request,
        "publishing/plan_item_form.html",
        {
            "account": account,
            "item": item,
            "pillars": pillars,
            "campaigns": campaigns,
            "media_type_choices": ContentPost.MediaType.choices,
            "plan_status_choices": ContentPlanItem.PlanStatus.choices,
        },
    )


def _save_plan_item(request, account, instance=None):
    """Create or update a ContentPlanItem from POST data. Returns instance or None on error."""
    topic = request.POST.get("topic", "").strip()
    if not topic:
        messages.error(request, "Topic is required.")
        return None

    objective = request.POST.get("objective", "").strip()
    platforms = request.POST.getlist("platforms")
    media_type = request.POST.get("media_type", "image")
    plan_status = request.POST.get("plan_status", ContentPlanItem.PlanStatus.IDEA)
    notes = request.POST.get("notes", "").strip()
    planned_date_raw = request.POST.get("planned_date", "").strip()
    planned_date = None
    if planned_date_raw:
        try:
            planned_date = datetime.date.fromisoformat(planned_date_raw)
        except ValueError:
            messages.error(request, "Invalid planned date.")
            return None

    pillar_id = request.POST.get("pillar", "")
    campaign_id = request.POST.get("campaign", "")

    pillar = None
    if pillar_id:
        pillar = ContentPillar.objects.filter(pk=pillar_id, account=account).first()

    campaign = None
    if campaign_id:
        campaign = ContentCampaign.objects.filter(
            pk=campaign_id, account=account
        ).first()

    if instance is None:
        instance = ContentPlanItem(account=account)

    instance.topic = topic
    instance.objective = objective
    instance.platforms = platforms
    instance.media_type = media_type
    instance.plan_status = plan_status
    instance.notes = notes
    instance.planned_date = planned_date
    instance.pillar = pillar
    instance.campaign = campaign
    instance.save()
    return instance


@login_required
@require_POST
def plan_item_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    item = get_object_or_404(ContentPlanItem, pk=pk, account=account)
    if item.plan_status not in (
        ContentPlanItem.PlanStatus.IDEA,
        ContentPlanItem.PlanStatus.PLANNED,
    ):
        messages.error(request, "Only idea or planned items can be deleted.")
        return redirect("publishing:planning")

    item.delete()
    messages.success(request, "Plan item deleted.")
    return redirect("publishing:planning")


@login_required
@require_POST
def plan_item_create_post(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    item = get_object_or_404(ContentPlanItem, pk=pk, account=account)

    with transaction.atomic():
        post = ContentPost.objects.create(
            account=account,
            plan_item=item,
            platforms=item.platforms,
            media_type=item.media_type,
            status=ContentPost.Status.DRAFT,
            created_by=request.user,
        )
        item.plan_status = ContentPlanItem.PlanStatus.IN_PRODUCTION
        item.save(update_fields=["plan_status", "updated_at"])

    return redirect(
        f"{request.build_absolute_uri('/publishing/new/')}?post_id={post.pk}"
    )


@login_required
def pillar_manage(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        color = request.POST.get("color", "#6366f1").strip()
        if name:
            ContentPillar.objects.create(account=account, name=name, color=color)
            messages.success(request, f'Pillar "{name}" created.')
        else:
            messages.error(request, "Pillar name is required.")
        return redirect("publishing:pillars")

    pillars = ContentPillar.objects.filter(account=account)
    return render(
        request,
        "publishing/pillars.html",
        {"account": account, "pillars": pillars},
    )


@login_required
def campaign_manage(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        objective = request.POST.get("objective", "").strip()
        if name:
            ContentCampaign.objects.create(
                account=account, name=name, objective=objective
            )
            messages.success(request, f'Campaign "{name}" created.')
        else:
            messages.error(request, "Campaign name is required.")
        return redirect("publishing:campaigns")

    campaigns = ContentCampaign.objects.filter(account=account)
    return render(
        request,
        "publishing/campaigns.html",
        {"account": account, "campaigns": campaigns},
    )


# ---------------------------------------------------------------------------
# Content Post CRUD
# ---------------------------------------------------------------------------


_ALLOWED_MEDIA_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/quicktime",
    "video/x-msvideo",
}
_MAX_MEDIA_BYTES = 100 * 1024 * 1024  # 100 MB


@login_required
@require_POST
def post_media_upload(request):
    """Upload a single media file for a post. Returns {url, name, content_type}."""
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "Not found"}, status=404)

    f = request.FILES.get("file")
    if f is None:
        return JsonResponse({"error": "No file provided."}, status=400)
    if f.content_type not in _ALLOWED_MEDIA_TYPES:
        return JsonResponse(
            {"error": f'"{f.content_type}" is not a supported media type.'}, status=400
        )
    if f.size > _MAX_MEDIA_BYTES:
        return JsonResponse({"error": "File exceeds the 100 MB limit."}, status=400)

    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    path = default_storage.save(
        f"publishing/{account.pk}/{f.name}", ContentFile(f.read())
    )
    url = default_storage.url(path)
    # Ensure the URL is absolute so Meta's API can fetch it.
    # S3/CloudFront URLs are already absolute; local dev URLs need the host prepended.
    if url.startswith("/"):
        url = request.build_absolute_uri(url)
    return JsonResponse({"url": url, "name": f.name, "content_type": f.content_type})


@login_required
def post_new(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    # Load existing draft if arriving from plan_item_create_post
    existing_post = None
    post_id_param = request.GET.get("post_id", "")
    if post_id_param:
        try:
            existing_post = ContentPost.objects.get(
                pk=int(post_id_param), account=account
            )
        except (ContentPost.DoesNotExist, ValueError):
            pass

    if request.method == "POST":
        pk = request.POST.get("post_id", "")
        if pk:
            try:
                post = ContentPost.objects.get(pk=int(pk), account=account)
            except (ContentPost.DoesNotExist, ValueError):
                post = ContentPost(account=account, created_by=request.user)
        else:
            post = ContentPost(account=account, created_by=request.user)

        caption = request.POST.get("caption", "").strip()
        hashtags_raw = request.POST.get("hashtags", "").strip()
        hashtags = [t.lstrip("#").strip() for t in hashtags_raw.split() if t.strip()]
        platforms = request.POST.getlist("platforms")
        media_type = request.POST.get("media_type", ContentPost.MediaType.IMAGE)
        # Accept either a JSON array (from the uploader) or newline-separated URLs (legacy)
        import json as _json

        media_urls_json = request.POST.get("media_urls_json", "").strip()
        if media_urls_json:
            try:
                media_urls = [
                    u
                    for u in _json.loads(media_urls_json)
                    if isinstance(u, str) and u.strip()
                ]
            except (ValueError, TypeError):
                media_urls = []
        else:
            media_urls_raw = request.POST.get("media_urls", "").strip()
            media_urls = [u.strip() for u in media_urls_raw.splitlines() if u.strip()]
        scheduled_at_raw = request.POST.get("scheduled_at", "").strip()

        # Platform-specific caption overrides
        platform_captions = {}
        for plat in platforms:
            override = request.POST.get(f"caption_{plat}", "").strip()
            if override:
                platform_captions[plat] = override

        scheduled_at = None
        if scheduled_at_raw:
            from django.utils.dateparse import parse_datetime

            scheduled_at = parse_datetime(scheduled_at_raw)
            if scheduled_at and timezone.is_naive(scheduled_at):
                scheduled_at = timezone.make_aware(scheduled_at)

        action = request.POST.get("action", "draft")
        if action == "schedule" and scheduled_at:
            status = ContentPost.Status.SCHEDULED
        elif action == "publish_now":
            status = ContentPost.Status.PUBLISHING
        else:
            status = ContentPost.Status.DRAFT

        post.caption = caption
        post.hashtags = hashtags
        post.platforms = platforms
        post.media_type = media_type
        post.media_urls = media_urls
        post.scheduled_at = scheduled_at
        post.status = status
        post.platform_captions = platform_captions
        if not post.pk:
            post.created_by = request.user
        post.save()

        if action == "publish_now":
            from .tasks import publish_post_async

            publish_post_async.delay(post.pk)

        messages.success(request, "Post saved.")
        return redirect("publishing:post-detail", pk=post.pk)

    goal_choices = [
        ("engagement", "Increase engagement"),
        ("sales", "Drive sales"),
        ("awareness", "Build brand awareness"),
        ("educate", "Educate customers"),
    ]
    return render(
        request,
        "publishing/post_new.html",
        {
            "account": account,
            "media_types": ContentPost.MediaType.choices,
            "existing_post": existing_post,
            "goal_choices": goal_choices,
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
    if post.status not in (
        ContentPost.Status.DRAFT,
        ContentPost.Status.SCHEDULED,
        ContentPost.Status.IDEA,
    ):
        messages.error(request, "Only draft or scheduled posts can be deleted.")
        return redirect("publishing:post-detail", pk=post.pk)

    post.delete()
    messages.success(request, "Post deleted.")
    return redirect("publishing:index")


@login_required
@require_POST
def post_retry(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)
    if post.status != ContentPost.Status.FAILED:
        messages.error(request, "Only failed posts can be retried.")
        return redirect("publishing:post-detail", pk=post.pk)

    # Validate scheduled_at is in the future (or allow override via POST)
    scheduled_at_raw = request.POST.get("scheduled_at", "").strip()
    if scheduled_at_raw:
        from django.utils.dateparse import parse_datetime

        new_scheduled = parse_datetime(scheduled_at_raw)
        if new_scheduled and timezone.is_naive(new_scheduled):
            new_scheduled = timezone.make_aware(new_scheduled)
        post.scheduled_at = new_scheduled

    if not post.scheduled_at or post.scheduled_at <= timezone.now():
        # Default to 2 minutes from now
        post.scheduled_at = timezone.now() + datetime.timedelta(minutes=2)

    post.status = ContentPost.Status.SCHEDULED
    post.error = ""
    post.save(update_fields=["status", "error", "scheduled_at"])
    messages.success(request, "Post rescheduled for retry.")
    return redirect("publishing:post-detail", pk=post.pk)


@login_required
@require_POST
def post_cancel(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)
    if post.status != ContentPost.Status.SCHEDULED:
        messages.error(request, "Only scheduled posts can be cancelled.")
        return redirect("publishing:post-detail", pk=post.pk)

    post.status = ContentPost.Status.CANCELLED
    post.save(update_fields=["status"])
    messages.success(request, "Post cancelled.")
    return redirect("publishing:post-detail", pk=post.pk)


@login_required
@require_POST
def post_reopen(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)
    if post.status != ContentPost.Status.CANCELLED:
        messages.error(request, "Only cancelled posts can be reopened.")
        return redirect("publishing:post-detail", pk=post.pk)

    post.status = ContentPost.Status.DRAFT
    post.scheduled_at = None
    post.save(update_fields=["status", "scheduled_at"])
    messages.success(request, "Post reopened as draft.")
    return redirect("publishing:post-detail", pk=post.pk)


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
            post = ContentPost.objects.get(pk=int(post_id), account=account)
            ContentCaption.objects.create(
                post=post,
                prompt=prompt,
                body="",
                hashtags=[],
            )
        except (ContentPost.DoesNotExist, ValueError):
            pass

    return JsonResponse({"draft_id": draft_id})


@login_required
def caption_draft_result(request, draft_id):
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
