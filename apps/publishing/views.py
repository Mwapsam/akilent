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
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from apps.accounts.api import is_account_admin
from apps.accounts.fields import open_text, seal_text
from apps.accounts.utils import get_current_account
from apps.core.htmx import full_page_load

from .composer import (
    EDITABLE_STATUSES as _EDITABLE_POST_STATUSES,
)
from .composer import (
    parse_schedule as _parse_schedule,
)
from .models import (
    ContentCampaign,
    ContentCaption,
    ContentPillar,
    ContentPlanItem,
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
    PublishingSettings,
)
from .providers.facebook import (
    FacebookPublishError,
    FacebookPublishProvider,
    exchange_for_long_lived_user_token,
    get_page_token,
    list_pages,
)

logger = logging.getLogger(__name__)

_FB_GRAPH = "https://graph.facebook.com"
# One Graph version for the whole publishing flow (OAuth and the providers).
_FB_GRAPH_VERSION = settings.INSTAGRAM_GRAPH_VERSION
_FB_OAUTH_STATE_TTL = datetime.timedelta(minutes=10)
_FB_PAGE_CHOICES_KEY = "fb_page_choices"


# Goals the composer's AI brief offers; the generator only accepts these.
CAPTION_GOAL_CHOICES = [
    ("engagement", "Increase engagement"),
    ("sales", "Drive sales"),
    ("awareness", "Build brand awareness"),
    ("educate", "Educate customers"),
]


def _admin_required(request, account, redirect_to: str):
    """Return a redirect when the user is not an Owner/Admin of ``account``, else None."""
    if is_account_admin(request.user, account):
        return None
    messages.error(request, "Only an Owner or Admin can do that.")
    return redirect(redirect_to)


def _in_workspace_tz(view):
    """Show and read schedule times in the business's own timezone (from its opening
    hours), so a time picked in the composer is the time on the owner's clock."""
    from functools import wraps
    from zoneinfo import ZoneInfo

    from apps.accounts.business_hours import workspace_timezone

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        account = get_current_account(request)
        if account is None:
            return view(request, *args, **kwargs)
        with timezone.override(ZoneInfo(workspace_timezone(account))):
            return view(request, *args, **kwargs)

    return wrapper


# read_insights + pages_read_engagement: post views, reach and reactions (Page/insights).
# pages_manage_engagement: a post's first comment.
_FB_SCOPES = (
    "pages_show_list,pages_manage_posts,pages_read_engagement,read_insights,"
    "pages_manage_engagement,business_management"
)

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
    if (
        denied := _admin_required(request, account, "settings-integrations")
    ) is not None:
        return denied

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

    graph_version = _FB_GRAPH_VERSION
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
    if (
        denied := _admin_required(request, account, "settings-integrations")
    ) is not None:
        return denied

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

    # A completed connect clears oauth_state to "", so an empty state must never match.
    if not state or conn.oauth_state != state:
        messages.error(request, "Invalid state parameter.")
        return redirect("settings-integrations")
    state_age = timezone.now() - (conn.oauth_state_created_at or timezone.now())
    # Consume the state immediately so a callback URL can never be replayed.
    conn.oauth_state = ""
    conn.oauth_state_created_at = None
    conn.save(update_fields=["oauth_state", "oauth_state_created_at", "updated_at"])
    if state_age > _FB_OAUTH_STATE_TTL:
        messages.error(
            request, "The Facebook connection took too long. Please try again."
        )
        return redirect("settings-integrations")

    app_id = getattr(settings, "WHATSAPP_APP_ID", "")
    app_secret = getattr(settings, "WHATSAPP_APP_SECRET", "")

    r = requests.get(
        f"{_FB_GRAPH}/{_FB_GRAPH_VERSION}/oauth/access_token",
        params={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    try:
        token_data = r.json()
    except ValueError:
        logger.warning("Facebook code exchange returned non-JSON: %s", r.status_code)
        messages.error(
            request, "Facebook didn't respond as expected. Please try again."
        )
        return redirect("settings-integrations")
    if "error" in token_data:
        messages.error(
            request,
            f"Facebook token exchange failed: {token_data['error'].get('message', '')}",
        )
        return redirect("settings-integrations")

    # The code exchange yields a ~1-hour user token, and page tokens fetched with it expire
    # with it. Upgrade first so the stored page token does not expire.
    try:
        user_token = exchange_for_long_lived_user_token(
            app_id, app_secret, token_data["access_token"]
        )
    except FacebookPublishError as exc:
        messages.error(request, f"Facebook token exchange failed: {exc}")
        return redirect("settings-integrations")

    try:
        pages = list_pages(user_token)
    except FacebookPublishError as exc:
        messages.error(request, f"Could not fetch Facebook Pages: {exc}")
        return redirect("settings-integrations")

    if not pages:
        messages.error(request, "No Facebook Pages found for this account.")
        return redirect("settings-integrations")

    if len(pages) == 1:
        page = pages[0]
        _activate_fb_page(conn, page["id"], page.get("name", ""), page["access_token"])
        messages.success(request, f"Connected to Facebook Page: {conn.page_name}")
        return redirect("settings-integrations")

    # Several Pages: let the user choose instead of silently connecting the first one.
    # The user token is sealed before it is kept in the session.
    request.session[_FB_PAGE_CHOICES_KEY] = {
        "user_token": seal_text(user_token),
        "pages": [{"id": p["id"], "name": p.get("name", "")} for p in pages],
        "created_at": timezone.now().isoformat(),
    }
    return redirect("publishing:fb-select-page")


def _activate_fb_page(conn, page_id: str, page_name: str, page_token: str) -> None:
    conn.page_id = page_id
    conn.page_name = page_name
    conn.access_token = page_token
    conn.token_expires_at = (
        None  # Page tokens from a long-lived user token never expire
    )
    conn.oauth_state = ""
    conn.status = FacebookPageConnection.Status.ACTIVE
    conn.save()


@full_page_load
@login_required
def fb_select_page(request):
    """Pick which Facebook Page to connect when the user manages more than one."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if (
        denied := _admin_required(request, account, "settings-integrations")
    ) is not None:
        return denied

    choices = request.session.get(_FB_PAGE_CHOICES_KEY)
    created_at = parse_datetime((choices or {}).get("created_at", ""))
    if (
        not choices
        or not created_at
        or timezone.now() - created_at > _FB_OAUTH_STATE_TTL
    ):
        request.session.pop(_FB_PAGE_CHOICES_KEY, None)
        messages.error(
            request, "Page selection expired. Please connect Facebook again."
        )
        return redirect("settings-integrations")

    if request.method == "POST":
        page_id = request.POST.get("page_id", "")
        page = next((p for p in choices["pages"] if p["id"] == page_id), None)
        user_token = open_text(choices["user_token"])
        if page is None or not user_token:
            messages.error(request, "Choose one of the listed Pages.")
            return redirect("publishing:fb-select-page")
        try:
            page_token = get_page_token(user_token, page_id)
        except FacebookPublishError as exc:
            messages.error(request, f"Could not connect that Page: {exc}")
            return redirect("publishing:fb-select-page")
        conn, _ = FacebookPageConnection.objects.get_or_create(account=account)
        _activate_fb_page(conn, page_id, page["name"], page_token)
        request.session.pop(_FB_PAGE_CHOICES_KEY, None)
        messages.success(request, f"Connected to Facebook Page: {conn.page_name}")
        return redirect("settings-integrations")

    return render(
        request,
        "publishing/fb_select_page.html",
        {"account": account, "pages": choices["pages"]},
    )


@login_required
@require_POST
def fb_oauth_disconnect(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if (
        denied := _admin_required(request, account, "settings-integrations")
    ) is not None:
        return denied

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
@_in_workspace_tz
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


def _calendar_entries(account, start, end) -> list[dict]:
    """Calendar entries for posts with a time in [start, end], one per publish time.

    A post whose channels share a time is one entry. A post with a different time per
    channel shows one entry per channel time, each with that channel's own status.
    """
    variants = (
        ContentPostVariant.objects.filter(
            post__account=account, scheduled_at__gte=start, scheduled_at__lte=end
        )
        .exclude(status=ContentPostVariant.Status.CANCELLED)
        .select_related("post")
    )
    by_post: dict[int, list] = {}
    for v in variants:
        if v.platform in (v.post.platforms or []):
            by_post.setdefault(v.post_id, []).append(v)

    entries = []
    for post_variants in by_post.values():
        post = post_variants[0].post
        times = {v.scheduled_at for v in post_variants}
        if len(times) == 1 and len(post_variants) == len(post.platforms or []):
            entries.append(
                {
                    "post": post,
                    "at": post_variants[0].scheduled_at,
                    "platforms": post.platforms,
                    "status": post.status,
                    "status_label": post.get_status_display(),
                }
            )
            continue
        for v in post_variants:
            entries.append(
                {
                    "post": post,
                    "at": v.scheduled_at,
                    "platforms": [v.platform],
                    "status": v.status,
                    "status_label": v.get_status_display(),
                }
            )

    # Posts scheduled only at post level (no variant time in range) keep one entry.
    for post in ContentPost.objects.filter(
        account=account, scheduled_at__gte=start, scheduled_at__lte=end
    ).exclude(pk__in=by_post.keys()):
        if (
            post.variants.exclude(status=ContentPostVariant.Status.CANCELLED)
            .filter(scheduled_at__isnull=False)
            .exclude(scheduled_at=post.scheduled_at)
            .exists()
        ):
            continue  # its channels are scheduled in another week
        entries.append(
            {
                "post": post,
                "at": post.scheduled_at,
                "platforms": post.platforms or [],
                "status": post.status,
                "status_label": post.get_status_display(),
            }
        )
    entries.sort(key=lambda e: e["at"])
    return entries


@login_required
@_in_workspace_tz
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
    this_iso = week_start.isocalendar()
    week_param = f"{this_iso[0]}-W{this_iso[1]:02d}"

    # Server-side filters
    channel_filter = request.GET.get("channel", "")
    status_filter = request.GET.get("status", "")
    view_mode = request.GET.get("view", "week")

    entries = _calendar_entries(account, week_start_dt, week_end_dt)
    if channel_filter:
        entries = [e for e in entries if channel_filter in e["platforms"]]
    if status_filter:
        entries = [e for e in entries if e["post"].status == status_filter]

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
            e for e in entries if timezone.localtime(e["at"], tz).date() == day
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
            "week_param": week_param,
            "view_mode": view_mode,
            "channel_filter": channel_filter,
            "status_filter": status_filter,
            "unscheduled_drafts": unscheduled_drafts,
            "status_choices": ContentPost.Status.choices,
            "today": today,
            "week_has_content": any(items or posts for _, items, posts in week_days),
        },
    )


# ---------------------------------------------------------------------------
# Content Library
# ---------------------------------------------------------------------------


@login_required
@_in_workspace_tz
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
    posts = list(qs.prefetch_related("variants"))
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
        _, per_model = ContentPost.objects.filter(
            pk__in=pks,
            account=account,
            status__in=[ContentPost.Status.IDEA, ContentPost.Status.DRAFT],
        ).delete()
        deleted = per_model.get(ContentPost._meta.label, 0)
        skipped = len(pks) - deleted
        if deleted:
            messages.success(request, f"Deleted {deleted} draft(s).")
        if skipped:
            messages.warning(
                request,
                f"{skipped} selected post(s) were not deleted. Only ideas and drafts can be deleted.",
            )

    elif action == "cancel_scheduled":
        from .services import cancel_post

        to_cancel = list(
            ContentPost.objects.filter(
                pk__in=pks,
                account=account,
                status=ContentPost.Status.SCHEDULED,
            )
        )
        with transaction.atomic():
            for scheduled_post in to_cancel:
                cancel_post(scheduled_post)
        updated = len(to_cancel)
        if updated:
            messages.success(request, f"Cancelled {updated} scheduled post(s).")
        skipped = len(pks) - updated
        if skipped:
            messages.warning(
                request,
                f"{skipped} selected post(s) were not cancelled. Only scheduled posts can be cancelled.",
            )

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
    ig_publish_scope = bool(ig and ig.access_token and not ig.token_expired)

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
            "requires_review": PublishingSettings.requires_review_for(account),
            "is_admin": is_account_admin(request.user, account),
        },
    )


@login_required
@require_POST
def publishing_settings_update(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if (denied := _admin_required(request, account, "publishing:channels")) is not None:
        return denied

    prefs, _ = PublishingSettings.objects.get_or_create(account=account)
    prefs.requires_review = request.POST.get("requires_review") == "on"
    prefs.save(update_fields=["requires_review", "updated_at"])
    messages.success(
        request,
        "Posts now need an Owner or Admin approval before publishing."
        if prefs.requires_review
        else "Review before publishing is off.",
    )
    return redirect("publishing:channels")


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

    from .services import initialize_post_with_variants

    try:
        with transaction.atomic():
            post = ContentPost.objects.create(
                account=account,
                plan_item=item,
                platforms=item.platforms,
                media_type=item.media_type,
                status=ContentPost.Status.DRAFT,
                created_by=request.user,
                brief_objective=item.objective,
            )
            item.plan_status = ContentPlanItem.PlanStatus.IN_PRODUCTION
            item.save(update_fields=["plan_status", "updated_at"])
            initialize_post_with_variants(post, item.platforms)
    except ValueError as exc:
        messages.error(request, f"Cannot create a post from this plan item: {exc!s}")
        return redirect("publishing:planning")

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


def _theme_date(raw: str):
    from datetime import date

    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _save_theme(request, account) -> None:
    """Create, edit or delete a content theme (ContentCampaign) from the themes page."""
    action = request.POST.get("action") or "create"
    if action in ("update", "delete"):
        theme = ContentCampaign.objects.filter(
            account=account, pk=request.POST.get("pk") or 0
        ).first()
        if theme is None:
            messages.error(request, "That theme no longer exists.")
            return
        if action == "delete":
            name = theme.name
            theme.delete()  # posts and plan items keep going, without the theme
            messages.success(
                request, f"Deleted the theme “{name}”. Its posts are kept."
            )
            return
    else:
        theme = ContentCampaign(account=account)
    name = request.POST.get("name", "").strip()[:200]
    if not name:
        messages.error(request, "Give the theme a name.")
        return
    start = _theme_date(request.POST.get("start_date", ""))
    end = _theme_date(request.POST.get("end_date", ""))
    if start and end and end < start:
        messages.error(request, "The end date is before the start date.")
        return
    theme.name = name
    theme.objective = request.POST.get("objective", "").strip()
    theme.start_date, theme.end_date = start, end
    theme.save()
    messages.success(
        request, f"Saved “{name}”." if action == "update" else f"Added “{name}”."
    )


@login_required
def campaign_manage(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        _save_theme(request, account)
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


def _post_new_submit(request, account):
    """The composer's buttons: save a draft, submit for review, schedule, publish now."""
    from . import readiness
    from .composer import (
        ComposerError,
        create_or_get,
        find_post,
        parse_client_key,
        parse_form,
        save_new_or_existing,
    )

    client_key = parse_client_key(request.POST.get("client_key"))
    post = find_post(account, request.POST.get("post_id"), client_key)
    if post is None:
        post, _ = create_or_get(account, request.user, client_key)

    if post.pk and post.status not in _EDITABLE_POST_STATUSES:
        if client_key and post.client_key == client_key:
            # The same composer page sent twice (a double click, a resubmitted form).
            messages.info(
                request, "This post was already sent. Here's where it stands."
            )
        else:
            messages.error(
                request,
                f"A {post.get_status_display().lower()} post can't be edited.",
            )
        return redirect("publishing:post-detail", pk=post.pk)

    back = redirect(
        f"{reverse('publishing:post-new')}?post_id={post.pk}"
        if post.pk
        else "publishing:post-new"
    )
    action = request.POST.get("action", "draft")
    try:
        inp = parse_form(request.POST)
    except ComposerError as exc:
        messages.error(request, str(exc))
        return back

    # The same check the composer shows as the user edits. A post that still needs a
    # review is only saved as a draft whatever the button, so only its content is checked.
    if action in readiness.GATED_ACTIONS:
        requires_review = (
            post.requires_approval
            if post.pk
            else PublishingSettings.requires_review_for(account)
        )
        gate = action
        if requires_review and not (post.pk and post.approval_status == "approved"):
            gate = readiness.SUBMIT_REVIEW
        blockers = readiness.blocking(readiness.check(inp, account), gate)
        if blockers:
            for issue in blockers:
                messages.error(request, issue["message"])
            return back

    try:
        result = save_new_or_existing(post, inp, action, request.user, request.POST)
    except ValueError as exc:
        messages.error(request, f"Failed to initialize post: {exc!s}")
        return redirect("publishing:post-new")
    post = result.post
    if result.duplicate:
        messages.info(request, "This post was already sent. Here's where it stands.")
        return redirect("publishing:post-detail", pk=post.pk)
    if result.missing_wa_number:
        messages.warning(
            request,
            "No WhatsApp chat link was added: connect a WhatsApp number first.",
        )

    if action == "submit_review":
        if post.requires_approval and _submit_for_review(post, request.user):
            messages.success(request, "Post saved and submitted for review.")
        elif post.approval_status == "submitted":
            messages.success(request, "Post saved. It's still waiting for review.")
        else:
            messages.success(request, "Post saved.")
        return redirect("publishing:post-detail", pk=post.pk)

    if result.review_blocked:
        messages.warning(
            request,
            "This workspace requires review before publishing. The post was saved as a "
            "draft; submit it for review, then schedule or publish it once approved.",
        )
        return redirect("publishing:post-detail", pk=post.pk)

    if action == "publish_now":
        from .tasks import publish_post_async

        try:
            publish_post_async.delay(post.pk)
        except Exception:
            logger.exception("Failed to enqueue publish for post=%s", post.pk)
            post.status = ContentPost.Status.DRAFT
            post.save(update_fields=["status", "updated_at"])
            messages.error(
                request,
                "Couldn't start publishing right now. The post was saved as a draft; please try again.",
            )
            return redirect("publishing:post-detail", pk=post.pk)
        messages.success(
            request, "Publishing has started. Each channel's result shows below."
        )
        return redirect("publishing:post-detail", pk=post.pk)

    messages.success(request, "Post saved.")
    return redirect("publishing:post-detail", pk=post.pk)


@login_required
@require_POST
@_in_workspace_tz
def composer_readiness(request):
    """What stops the composer's post from going out, as the user edits it."""
    from . import readiness
    from .composer import ComposerError, parse_form

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "Not found"}, status=404)
    try:
        issues = readiness.check(parse_form(request.POST), account)
    except ComposerError as exc:
        issues = readiness.check(None, account, error=exc)
    return JsonResponse({"issues": issues})


@login_required
@require_POST
@_in_workspace_tz
def composer_autosave(request):
    """Save the composer's draft in the background.

    Only a draft is saved this way: a scheduled or in-review post changes only when the
    user presses a button. ``version`` is the copy the page last saw; when someone saved
    the post since, the save is refused (409) unless the page sends ``force``.
    """
    from .composer import (
        AUTOSAVE_STATUSES,
        ComposerError,
        StaleVersion,
        create_or_get,
        find_post,
        parse_client_key,
        parse_form,
        save_new_or_existing,
        version_of,
    )
    from .links import links_for_post, short_url

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "Not found"}, status=404)

    client_key = parse_client_key(request.POST.get("client_key"))
    post_id = request.POST.get("post_id")
    if not post_id and not client_key:
        return JsonResponse({"saved": False, "reason": "no_key"}, status=400)
    post = find_post(account, post_id, client_key)

    if post is not None:
        if post.status not in AUTOSAVE_STATUSES:
            return JsonResponse(
                {
                    "saved": False,
                    "reason": "not_draft",
                    "message": f"This post is {post.get_status_display().lower()}, so "
                    "changes are saved only when you press a button.",
                },
                status=409,
            )
        version = request.POST.get("version", "")
        if version and not request.POST.get("force") and version != version_of(post):
            return JsonResponse(
                {"saved": False, "reason": "conflict", "version": version_of(post)},
                status=409,
            )

    try:
        inp = parse_form(request.POST)
    except ComposerError as exc:
        return JsonResponse(
            {
                "saved": False,
                "reason": "invalid",
                "message": str(exc),
                "field": exc.field,
            },
            status=422,
        )
    if not inp.platforms:
        return JsonResponse({"saved": False, "reason": "no_destination"})
    if post is None:
        if not (inp.caption or inp.media_urls or any(inp.platform_captions.values())):
            return JsonResponse({"saved": False, "reason": "empty"})
        post, _ = create_or_get(account, request.user, client_key)

    # The early check above is a fast path; this one runs under the row lock.
    expected = "" if request.POST.get("force") else request.POST.get("version", "")
    try:
        result = save_new_or_existing(
            post,
            inp,
            "draft",
            request.user,
            request.POST,
            editable=AUTOSAVE_STATUSES,
            expected_version=expected,
        )
    except StaleVersion as exc:
        return JsonResponse(
            {"saved": False, "reason": "conflict", "version": str(exc)}, status=409
        )
    if result.duplicate:
        return JsonResponse(
            {"saved": False, "reason": "conflict", "version": version_of(result.post)},
            status=409,
        )
    post = result.post
    return JsonResponse(
        {
            "saved": True,
            "post_id": post.pk,
            "version": version_of(post),
            "wa_link_urls": {
                p: short_url(link) for p, link in links_for_post(post).items()
            },
        }
    )


@full_page_load
@login_required
@_in_workspace_tz
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
        if existing_post and existing_post.status not in _EDITABLE_POST_STATUSES:
            return redirect("publishing:post-detail", pk=existing_post.pk)

    if request.method == "POST":
        return _post_new_submit(request, account)

    requires_review = (
        existing_post.requires_approval
        if existing_post
        else PublishingSettings.requires_review_for(account)
    )
    approved = bool(
        existing_post
        and existing_post.approval_status == "approved"
        and existing_post.is_approval_valid()
    )
    # Prefill the schedule from the draft, or from the calendar's "+" link (?scheduled_at=).
    initial_scheduled = existing_post.scheduled_at if existing_post else None
    if initial_scheduled is None:
        initial_scheduled = parse_datetime(request.GET.get("scheduled_at", ""))
    elif timezone.is_aware(initial_scheduled):
        initial_scheduled = timezone.localtime(initial_scheduled)
    # Prefill each channel's own time; show the per-channel inputs when they differ.
    channel_times = {}
    if existing_post:
        for v in existing_post.variants.exclude(
            status=ContentPostVariant.Status.CANCELLED
        ):
            if v.scheduled_at:
                channel_times[v.platform] = timezone.localtime(v.scheduled_at).strftime(
                    "%Y-%m-%dT%H:%M"
                )
    import uuid

    from apps.accounts.business_hours import DEFAULT_TIMEZONE, workspace_timezone

    from . import constraints
    from .composer import AUTOSAVE_STATUSES, destinations, version_of
    from .links import (
        DEFAULT_LINK_TEXT,
        DEFAULT_PREFILL,
        DEFAULT_WHATSAPP_TEXT,
        INSTAGRAM_SUFFIX,
        cta_for,
        links_for_post,
        short_url,
        whatsapp_number_for,
    )
    from .post_options import caps_for_js

    wa_links = links_for_post(existing_post) if existing_post else {}
    dests = destinations(account)
    review_gate = requires_review and not approved
    return render(
        request,
        "publishing/post_new.html",
        {
            "account": account,
            "media_types": ContentPost.MediaType.choices,
            "existing_post": existing_post,
            "destinations": list(dests.values()),
            "editor_data": {
                "post_id": existing_post.pk if existing_post else None,
                # Ties autosaves and the final submit of a new post to one row.
                "client_key": str(
                    existing_post.client_key
                    if existing_post and existing_post.client_key
                    else uuid.uuid4()
                ),
                "version": version_of(existing_post) if existing_post else "",
                "status_label": existing_post.get_status_display()
                if existing_post
                else "New",
                "autosave": existing_post is None
                or existing_post.status in AUTOSAVE_STATUSES,
                "review_gate": review_gate,
                "constraints": constraints.for_js(),
                "destinations": dests,
                "tz_name": timezone.get_current_timezone_name(),
                "urls": {
                    "upload": reverse("publishing:post-media-upload"),
                    "readiness": reverse("publishing:composer-readiness"),
                    "autosave": reverse("publishing:composer-autosave"),
                    "locations": reverse("publishing:location-search"),
                    "caption_generate": reverse("publishing:caption-generate"),
                    "caption_result": reverse(
                        "publishing:caption-result", args=["PLACEHOLDER"]
                    ),
                    "edit": reverse("publishing:post-new"),
                },
                "caption": existing_post.caption if existing_post else "",
                "hashtags": existing_post.hashtags if existing_post else [],
                "platform_captions": existing_post.platform_captions
                if existing_post
                else {},
                "media_urls": existing_post.media_urls if existing_post else [],
                "platforms": existing_post.platforms
                if existing_post
                else ["instagram"],
                "media_type": existing_post.media_type
                if existing_post
                else ContentPost.MediaType.IMAGE,
                "scheduled_at": initial_scheduled.strftime("%Y-%m-%dT%H:%M")
                if initial_scheduled
                else "",
                "channel_times": channel_times,
                "per_channel": len(set(channel_times.values())) > 1,
                "cta": cta_for(existing_post) if existing_post else {},
                "options": {
                    v.platform: v.options
                    for v in existing_post.variants.exclude(
                        status=ContentPostVariant.Status.CANCELLED
                    )
                }
                if existing_post
                else {},
                "option_caps": caps_for_js(),
                "wa_link_urls": {p: short_url(link) for p, link in wa_links.items()},
                "cta_defaults": {
                    "whatsapp": DEFAULT_WHATSAPP_TEXT,
                    "link": DEFAULT_LINK_TEXT,
                    "prefill": DEFAULT_PREFILL,
                    "ig_suffix": INSTAGRAM_SUFFIX,
                    # Shown in the preview until the real short link exists.
                    "go_placeholder": (
                        (getattr(settings, "SITE_URL", "") or "").rstrip("/")
                        + "/go/······"
                    ),
                },
            },
            "goal_choices": CAPTION_GOAL_CHOICES,
            # Review-only actions until the post holds a still-valid approval; an approved
            # post can be scheduled, and editing its content sends it back for review.
            "review_gate": review_gate,
            "approved_for_publish": requires_review and approved,
            "current_tz_name": timezone.get_current_timezone_name(),
            "tz_is_default": workspace_timezone(account) == DEFAULT_TIMEZONE,
            "wa_number_ready": bool(whatsapp_number_for(account)),
            "wa_links": wa_links,
        },
    )


def _channel_options_for_display(post) -> list[dict]:
    """Each channel's saved options (only those valid for the post's media type) and
    its first comment's progress, for the post page."""
    from .post_options import supports
    from .services import FIRST_COMMENT_KEY

    rows = []
    for v in post.variants.exclude(status=ContentPostVariant.Status.CANCELLED):
        opts = {
            k: val
            for k, val in (v.options or {}).items()
            if supports(v.platform, k, post.media_type)
        }
        if not opts:
            continue
        rows.append(
            {
                "variant": v,
                "options": opts,
                "comment_state": (v.metadata or {}).get(FIRST_COMMENT_KEY) or {},
            }
        )
    return rows


@login_required
@_in_workspace_tz
def post_detail(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    from .links import cta_for, links_for_post, short_url
    from .results import results_for

    post = get_object_or_404(ContentPost, pk=pk, account=account)
    requires_approval = post.requires_approval
    cta_links = [
        (ContentPostVariant.Platform(platform).label, short_url(link))
        for platform, link in sorted(links_for_post(post).items())
    ]
    can_review = False
    if requires_approval and post.approval_status == "submitted":
        can_review, _ = _can_review(post, request.user, account)
    return render(
        request,
        "publishing/post_detail.html",
        {
            "account": account,
            "post": post,
            "editable": post.status in _EDITABLE_POST_STATUSES,
            "requires_approval": requires_approval,
            "approval_valid": post.is_approval_valid(),
            "can_review": can_review,
            "approval_logs": post.approval_logs.select_related("performed_by")[:20]
            if requires_approval
            else [],
            "results": results_for(post),
            "cta": cta_for(post),
            "cta_links": cta_links,
            "channel_options": _channel_options_for_display(post),
        },
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
@_in_workspace_tz
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
        post.scheduled_at = _parse_schedule(scheduled_at_raw)

    if not post.scheduled_at or post.scheduled_at <= timezone.now():
        # Default to 2 minutes from now
        post.scheduled_at = timezone.now() + datetime.timedelta(minutes=2)

    from .services import apply_schedule

    post.status = ContentPost.Status.SCHEDULED
    post.error = ""
    with transaction.atomic():
        post.save(update_fields=["status", "error", "scheduled_at"])
        # Every channel that hasn't gone out is retried at the new time.
        apply_schedule(post)
    messages.success(request, "Post rescheduled for retry.")
    return redirect("publishing:post-detail", pk=post.pk)


@login_required
@require_POST
def post_retry_variant(request, pk, platform):
    """Retry a specific failed variant/channel by enqueueing publish_variant_async task.

    Safeguards:
    - Only FAILED variants can be retried
    - Approval is revalidated when the task runs
    - Task enqueue failures are caught and displayed
    - Prevents duplicate enqueues via atomic state transition
    """
    from .tasks import publish_variant_async

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)

    try:
        variant = post.variants.get(platform=platform)
    except post.variants.model.DoesNotExist:
        messages.error(request, "Variant not found.")
        return redirect("publishing:post-detail", pk=post.pk)

    if variant.status != ContentPostVariant.Status.FAILED:
        messages.error(request, "Only failed variants can be retried.")
        return redirect("publishing:post-detail", pk=post.pk)

    # Atomically transition variant to PUBLISHING to prevent duplicate enqueues
    try:
        with transaction.atomic():
            # Reload to ensure no concurrent retry in progress
            variant = ContentPostVariant.objects.select_for_update().get(pk=variant.pk)

            # Double-check status (in case another request just retried)
            if variant.status != ContentPostVariant.Status.FAILED:
                messages.error(
                    request,
                    "This variant is already being processed or has been retried.",
                )
                return redirect("publishing:post-detail", pk=post.pk)

            # Transition to PUBLISHING atomically (marks the retry as in-progress)
            variant.status = ContentPostVariant.Status.PUBLISHING
            variant.last_error = ""
            variant.last_error_type = ""
            variant.last_error_at = None
            variant.last_attempt_at = timezone.now()
            variant.save(
                update_fields=[
                    "status",
                    "last_error",
                    "last_error_type",
                    "last_error_at",
                    "last_attempt_at",
                ]
            )

        # Enqueue the task after the atomic state transition succeeds
        try:
            publish_variant_async.delay(variant.pk)
        except Exception as enqueue_exc:
            # If enqueue fails, reset variant to FAILED so user can retry
            logger.exception(
                "Failed to enqueue publish_variant_async for variant=%s, resetting to FAILED",
                variant.pk,
            )
            variant.status = ContentPostVariant.Status.FAILED
            variant.last_error = f"Enqueue failed: {str(enqueue_exc)[:200]}"
            variant.last_error_type = ContentPostVariant.ErrorType.UNKNOWN
            variant.last_error_at = timezone.now()
            variant.save(
                update_fields=[
                    "status",
                    "last_error",
                    "last_error_type",
                    "last_error_at",
                ]
            )
            messages.error(
                request,
                "Failed to enqueue retry (broker unreachable). Variant reset to FAILED. Please try again.",
            )
            return redirect("publishing:post-detail", pk=post.pk)

    except Exception as exc:
        logger.exception("post_retry_variant failed for variant=%s", variant.pk)
        messages.error(
            request,
            f"Failed to retry: {str(exc)[:100]}. Please try again.",
        )
        return redirect("publishing:post-detail", pk=post.pk)

    messages.success(request, f"{platform.title()} publishing retry enqueued.")
    return redirect("publishing:post-detail", pk=post.pk)


@login_required
@require_POST
@_in_workspace_tz
def variant_reschedule(request, pk, platform):
    """Give one channel its own publish time, leaving the post's other channels as they are."""
    from .services import reschedule_variant

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    post = get_object_or_404(ContentPost, pk=pk, account=account)
    detail = redirect("publishing:post-detail", pk=post.pk)

    when = _parse_schedule(request.POST.get("scheduled_at", ""))
    if when is None:
        messages.error(request, "Choose a date and time for this channel.")
        return detail
    if when <= timezone.now():
        messages.error(request, "Choose a time in the future.")
        return detail
    if not post.is_approval_valid():
        messages.error(
            request, "This post needs an approved review before it can be scheduled."
        )
        return detail

    Status = ContentPostVariant.Status
    with transaction.atomic():
        post = ContentPost.objects.select_for_update().get(pk=post.pk)
        variant = post.variants.select_for_update().filter(platform=platform).first()
        if variant is None or not (
            platform in (post.platforms or [])
            and (
                variant.status == Status.SCHEDULED
                or (
                    variant.status == Status.DRAFT
                    and post.status
                    in (ContentPost.Status.SCHEDULED, ContentPost.Status.PUBLISHING)
                )
            )
        ):
            messages.error(
                request, "Only a channel that hasn't gone out can be rescheduled."
            )
            return detail
        variant.last_error = ""
        variant.save(update_fields=["last_error"])
        reschedule_variant(variant, when)

    from django.utils.formats import date_format

    local = timezone.localtime(when)
    messages.success(
        request,
        f"{variant.get_platform_display()} goes out "
        f"{date_format(local, 'N j')}, {local:%H:%M}.",
    )
    return detail


@login_required
@require_POST
def first_comment_retry(request, pk, variant_pk):
    """Queue a channel's failed first comment again."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    variant = get_object_or_404(
        ContentPostVariant, pk=variant_pk, post__pk=pk, post__account=account
    )
    from .services import retry_first_comment

    if retry_first_comment(variant):
        messages.success(
            request,
            f"Posting the first comment on {variant.get_platform_display()} again.",
        )
    else:
        messages.error(request, "That comment isn't waiting for a retry.")
    return redirect("publishing:post-detail", pk=pk)


@login_required
def location_search(request):
    """Places to tag on an Instagram post, from the Pages Search API.

    Uses the connected Facebook Page's token. Returns {"results": [...]} or
    {"error": "..."} with a message the composer shows as is.
    """
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "No account"}, status=403)
    query = " ".join(request.GET.get("q", "").split())[:100]
    if len(query) < 2:
        return JsonResponse({"results": []})

    conn = FacebookPageConnection.objects.filter(
        account=account, status=FacebookPageConnection.Status.ACTIVE
    ).first()
    if conn is None or not conn.access_token:
        return JsonResponse(
            {"error": "Connect your Facebook Page in Channels to search for locations."}
        )
    from .providers.errors import MetaPublishError

    try:
        pages = FacebookPublishProvider(
            conn.page_id, conn.access_token
        ).search_location_pages(query)
    except MetaPublishError as exc:
        logger.info("Location search failed for account=%s: %s", account.pk, exc)
        return JsonResponse(
            {
                "error": "Location search isn't available right now. You can post "
                "without a location."
            }
        )
    except Exception:
        logger.exception("Location search error for account=%s", account.pk)
        return JsonResponse({"error": "Couldn't reach Facebook. Try again."})

    results = []
    for page in pages:
        loc = page.get("location") or {}
        detail = ", ".join(
            part
            for part in (loc.get("street"), loc.get("city"), loc.get("country"))
            if part
        )
        results.append(
            {"id": str(page["id"]), "name": page.get("name", ""), "detail": detail}
        )
    return JsonResponse({"results": results})


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

    from .services import cancel_post

    with transaction.atomic():
        cancel_post(post)
    post.refresh_from_db(fields=["status"])
    if post.status == ContentPost.Status.CANCELLED:
        messages.success(request, "Post cancelled.")
    else:
        messages.success(
            request,
            "Channels still waiting were cancelled. Channels already live stay published.",
        )
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

    from .services import reopen_post

    with transaction.atomic():
        reopen_post(post)
    messages.success(request, "Post reopened as draft.")
    return redirect("publishing:post-detail", pk=post.pk)


@login_required
@require_POST
def post_refresh_insights(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)
    detail = redirect("publishing:post-detail", pk=post.pk)
    channels = list(
        post.variants.filter(
            status=ContentPostVariant.Status.PUBLISHED,
            platform__in=(
                ContentPostVariant.Platform.FACEBOOK,
                ContentPostVariant.Platform.INSTAGRAM,
            ),
        ).exclude(publication_id="")
    )
    if not channels:
        messages.error(request, "Numbers are available once a channel has published.")
        return detail

    # One manual read per channel every few minutes; Meta updates slowly and rate-limits.
    recent = timezone.now() - _METRICS_REFRESH_EVERY
    due = [
        v
        for v in channels
        if v.metrics_collected_at is None or v.metrics_collected_at < recent
    ]
    if not due:
        messages.info(request, "Numbers were just updated. Try again in a few minutes.")
        return detail

    from .tasks import collect_variant_metrics_task

    try:
        for variant in due:
            collect_variant_metrics_task.delay(variant.pk)
    except Exception:
        logger.exception("Failed to enqueue metrics refresh for post=%s", post.pk)
        messages.error(
            request, "Couldn't refresh the numbers right now. Please try again."
        )
        return detail
    messages.success(request, "Updating the numbers. Reload in a moment.")
    return detail


_METRICS_REFRESH_EVERY = datetime.timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Approval workflow
# ---------------------------------------------------------------------------


@login_required
@require_POST
def post_submit_for_review(request, pk):
    """Submit a draft post for review."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)

    if not _submit_for_review(post, request.user):
        messages.error(request, "Only draft posts can be submitted for review.")
        return redirect("publishing:post-detail", pk=post.pk)

    messages.success(request, "Post submitted for review.")
    return redirect("publishing:post-detail", pk=post.pk)


def _submit_for_review(post, user) -> bool:
    """Mark ``post`` submitted and log it. False when it is not a draft awaiting review."""
    # An approval of content that has since changed no longer counts, so it may be resubmitted.
    stale_approval = post.approval_status == "approved" and not post.is_approval_valid()
    if post.approval_status != "draft" and not stale_approval:
        return False

    post.approval_status = "submitted"
    post.save(update_fields=["approval_status"])

    from .models import ApprovalLog

    ApprovalLog.objects.create(
        post=post, action=ApprovalLog.Action.SUBMITTED, performed_by=user
    )
    return True


def _reviewer_ids(account) -> set[int]:
    """User ids of the workspace's Owners and Admins, the only people who may review."""
    from apps.accounts.models import Membership

    return set(
        Membership.objects.filter(
            account=account,
            role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
        ).values_list("user_id", flat=True)
    )


def _can_review(post, user, account, action="review") -> tuple[bool, str]:
    """Review policy for approve and request-changes alike. Only an Owner or Admin may review.
    The creator may review their own post only when no other Owner/Admin could. An assigned
    reviewer who is still an Owner/Admin takes precedence over everyone else."""
    reviewers = _reviewer_ids(account)
    if user.pk not in reviewers:
        return False, "Only an Owner or Admin can review posts."
    if post.created_by_id == user.pk and reviewers - {user.pk}:
        return (
            False,
            "You cannot review your own post while another Owner or Admin can. Ask them to review it.",
        )
    reviewer_id = post.assigned_reviewer_id
    if reviewer_id and reviewer_id in reviewers and reviewer_id != user.pk:
        return False, f"Only the assigned reviewer can {action} this post."
    return True, ""


@login_required
@require_POST
def post_approve(request, pk):
    """Approve a post for publishing."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)

    if post.approval_status not in ("submitted", "changes_requested"):
        messages.error(
            request, "Only submitted or changes-requested posts can be approved."
        )
        return redirect("publishing:post-detail", pk=post.pk)

    allowed, reason = _can_review(post, request.user, account, "approve")
    if not allowed:
        messages.error(request, reason)
        return redirect("publishing:post-detail", pk=post.pk)

    post.approval_status = "approved"
    post.approved_by = request.user
    post.approved_at = timezone.now()
    post.last_content_hash = post.compute_content_hash()
    post.save(
        update_fields=[
            "approval_status",
            "approved_by",
            "approved_at",
            "last_content_hash",
        ]
    )

    from .models import ApprovalLog

    ApprovalLog.objects.create(
        post=post, action=ApprovalLog.Action.APPROVED, performed_by=request.user
    )

    messages.success(request, "Post approved.")
    return redirect("publishing:post-detail", pk=post.pk)


@login_required
@require_POST
def post_request_changes(request, pk):
    """Request changes to a submitted post."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)

    if post.approval_status != "submitted":
        messages.error(request, "Can only request changes on submitted posts.")
        return redirect("publishing:post-detail", pk=post.pk)

    allowed, reason = _can_review(post, request.user, account, "request changes on")
    if not allowed:
        messages.error(request, reason)
        return redirect("publishing:post-detail", pk=post.pk)

    feedback = request.POST.get("feedback", "").strip()
    if not feedback:
        messages.error(request, "Feedback is required when requesting changes.")
        return redirect("publishing:post-detail", pk=post.pk)

    post.approval_status = "changes_requested"
    post.approval_feedback = feedback
    post.save(update_fields=["approval_status", "approval_feedback"])

    from .models import ApprovalLog

    ApprovalLog.objects.create(
        post=post,
        action=ApprovalLog.Action.CHANGES_REQUESTED,
        performed_by=request.user,
        feedback=feedback,
    )

    messages.success(request, "Changes requested.")
    return redirect("publishing:post-detail", pk=post.pk)


@login_required
@require_POST
def post_resubmit_after_changes(request, pk):
    """Resubmit a post after changes were requested."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    post = get_object_or_404(ContentPost, pk=pk, account=account)

    if post.approval_status != "changes_requested":
        messages.error(request, "Only posts with changes requested can be resubmitted.")
        return redirect("publishing:post-detail", pk=post.pk)

    if post.created_by != request.user:
        messages.error(request, "Only the creator can resubmit changes.")
        return redirect("publishing:post-detail", pk=post.pk)

    post.approval_status = "submitted"
    post.approval_feedback = ""
    post.save(update_fields=["approval_status", "approval_feedback"])

    from .models import ApprovalLog

    ApprovalLog.objects.create(
        post=post,
        action=ApprovalLog.Action.RESUBMITTED,
        performed_by=request.user,
    )

    messages.success(request, "Post resubmitted for review.")
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
    goal = dict(CAPTION_GOAL_CHOICES).get(request.POST.get("goal", ""), "")
    cta = " ".join(request.POST.get("cta", "").split())[:200]

    if not prompt:
        return JsonResponse({"error": "Prompt is required."}, status=400)

    from apps.ai import tasks as ai_tasks

    draft_id = ai_tasks.build_caption_draft(
        account.id, prompt, media_type, platform, goal=goal, cta=cta
    )

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
    options = result.get("options", [])
    first = options[0] if options else {}
    return JsonResponse(
        {
            "status": "ready",
            "options": options,
            "caption": first.get("caption", ""),
            "hashtags": first.get("hashtags", []),
        }
    )


# Reconciliation Operations (Step 4.7B)
# ---------------------------------------------------------------------------


@login_required
def reconciliation_queue(request):
    """List all variants in RECONCILING state for operator action."""
    from django.core.paginator import Paginator

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    # Only show variants for posts in this account
    reconciling_qs = (
        ContentPostVariant.objects.filter(
            post__account=account,
            status=ContentPostVariant.Status.RECONCILING,
        )
        .select_related("post")
        .order_by("-last_error_at")
    )

    paginator = Paginator(reconciling_qs, 20)  # 20 per page
    page = request.GET.get("page", 1)
    variants_page = paginator.get_page(page)

    return render(
        request,
        "publishing/reconciliation_queue.html",
        {
            "variants": variants_page,
            "account": account,
        },
    )


def _is_safe_public_url(url: str) -> bool:
    from django.core.exceptions import ValidationError
    from django.core.validators import URLValidator

    if not url.lower().startswith(("http://", "https://")):
        return False
    try:
        URLValidator(schemes=["http", "https"])(url)
    except ValidationError:
        return False
    return True


@login_required
@require_POST
def reconciliation_confirm_published(request, variant_id):
    """Operator confirms variant was actually published in provider."""
    from .services import reconcile_variant_confirmed_published

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if (
        denied := _admin_required(request, account, "publishing:reconciliation-queue")
    ) is not None:
        return denied

    # Phase A2: Do NOT acquire lock here; service owns the transaction.
    # View handles authorization and request parsing only.
    try:
        variant = ContentPostVariant.objects.get(pk=variant_id)
    except ContentPostVariant.DoesNotExist:
        messages.error(request, "Variant not found.")
        return redirect("publishing:reconciliation-queue")

    if variant.post.account != account:
        messages.error(request, "Access denied.")
        return redirect("publishing:reconciliation-queue")

    if variant.status != ContentPostVariant.Status.RECONCILING:
        messages.error(
            request,
            f"Only RECONCILING variants can be confirmed published. This variant is {variant.status}.",
        )
        return redirect("publishing:post-detail", pk=variant.post.pk)

    publication_id = request.POST.get("publication_id", "").strip()
    publication_url = request.POST.get("publication_url", "").strip()
    evidence_notes = request.POST.get("evidence_notes", "").strip()

    if not publication_id:
        messages.error(request, "Publication ID is required.")
        return redirect("publishing:reconciliation-queue")

    if publication_url and not _is_safe_public_url(publication_url):
        messages.error(
            request, "The published URL must start with http:// or https://."
        )
        return redirect("publishing:reconciliation-queue")

    try:
        reconcile_variant_confirmed_published(
            variant=variant,
            publication_id=publication_id,
            publication_url=publication_url,
            evidence_notes=evidence_notes,
            performed_by=request.user,
        )
        messages.success(
            request,
            f"{variant.platform.title()} variant confirmed published with ID {publication_id}.",
        )
    except Exception as exc:
        logger.exception("Failed to confirm published for variant=%s", variant_id)
        messages.error(request, f"Failed to confirm published: {str(exc)[:100]}")

    return redirect("publishing:post-detail", pk=variant.post.pk)


@login_required
@require_POST
def reconciliation_retry(request, variant_id):
    """Operator initiates retry for variant after determining it was not published."""
    from .services import reconcile_variant_retry

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if (
        denied := _admin_required(request, account, "publishing:reconciliation-queue")
    ) is not None:
        return denied

    # Phase A2: Do NOT acquire lock here; service owns the transaction.
    try:
        variant = ContentPostVariant.objects.get(pk=variant_id)
    except ContentPostVariant.DoesNotExist:
        messages.error(request, "Variant not found.")
        return redirect("publishing:reconciliation-queue")

    if variant.post.account != account:
        messages.error(request, "Access denied.")
        return redirect("publishing:reconciliation-queue")

    if variant.status != ContentPostVariant.Status.RECONCILING:
        messages.error(
            request,
            f"Only RECONCILING variants can be retried. This variant is {variant.status}.",
        )
        return redirect("publishing:post-detail", pk=variant.post.pk)

    try:
        reconcile_variant_retry(variant=variant, performed_by=request.user)
        messages.success(
            request,
            f"{variant.platform.title()} variant reset to FAILED and ready for retry.",
        )
    except Exception as exc:
        logger.exception("Failed to retry variant=%s", variant_id)
        messages.error(request, f"Failed to initiate retry: {str(exc)[:100]}")

    return redirect("publishing:post-detail", pk=variant.post.pk)


@login_required
@require_POST
def reconciliation_leave_unresolved(request, variant_id):
    """Operator leaves variant RECONCILING with notes for next investigation step."""
    from .services import reconcile_variant_leave_unresolved

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if (
        denied := _admin_required(request, account, "publishing:reconciliation-queue")
    ) is not None:
        return denied

    # Phase A2: Do NOT acquire lock here; service owns the transaction.
    try:
        variant = ContentPostVariant.objects.get(pk=variant_id)
    except ContentPostVariant.DoesNotExist:
        messages.error(request, "Variant not found.")
        return redirect("publishing:reconciliation-queue")

    if variant.post.account != account:
        messages.error(request, "Access denied.")
        return redirect("publishing:reconciliation-queue")

    if variant.status != ContentPostVariant.Status.RECONCILING:
        messages.error(
            request,
            f"Only RECONCILING variants can be left unresolved. This variant is {variant.status}.",
        )
        return redirect("publishing:post-detail", pk=variant.post.pk)

    next_step = request.POST.get("next_step", "").strip()
    if not next_step:
        messages.error(request, "Please describe the next investigative step.")
        return redirect("publishing:reconciliation-queue")

    try:
        reconcile_variant_leave_unresolved(
            variant=variant,
            next_step=next_step,
            performed_by=request.user,
        )
        messages.success(
            request,
            f"{variant.platform.title()} variant left unresolved. Next step: {next_step}",
        )
    except Exception as exc:
        logger.exception("Failed to leave unresolved for variant=%s", variant_id)
        messages.error(request, f"Failed to update: {str(exc)[:100]}")

    return redirect("publishing:reconciliation-queue")
