"""Scheduling pages (apps.publishing.scheduling). Behind the ``cs_scheduling`` release
gate: until it is released, ReleaseGateMiddleware 404s these URLs."""

from __future__ import annotations

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.api import is_account_admin
from apps.accounts.utils import get_current_account
from apps.core.htmx import overlay_or_page
from apps.core.module_gate import module_required

from . import scheduling
from .models import ContentPostVariant, PostingSlot
from .views import _in_workspace_tz


def _back(request):
    """The calendar page the move came from (same site only)."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect(f"{reverse('publishing:calendar')}?view=month")


@login_required
@module_required("content_studio")
@require_POST
@_in_workspace_tz
def calendar_move(request):
    """Move a post's channels to another day, keeping their times (drag and drop on
    the month calendar, or its Move form)."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        day = datetime.date.fromisoformat(request.POST.get("date", ""))
        post_pk = int(request.POST.get("post", ""))
    except ValueError:
        messages.error(request, "Choose a day to move the post to.")
        return _back(request)
    try:
        moved = scheduling.move_to_day(
            account, post_pk, request.POST.getlist("platforms"), day
        )
    except scheduling.SchedulingError as exc:
        messages.error(request, str(exc))
        return _back(request)
    from django.utils.formats import date_format

    local = timezone.localtime(moved[0].scheduled_at)
    messages.success(request, f"Moved to {date_format(local, 'l, N j')}.")
    return _back(request)


@login_required
@module_required("content_studio")
@require_POST
@_in_workspace_tz
def plan_move(request):
    """Move a planned idea to another day (drag and drop on the month calendar, or its
    Move form). Not to a day that has passed, and not an idea already published."""
    from django.utils.formats import date_format

    from .models import ContentPlanItem

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        day = datetime.date.fromisoformat(request.POST.get("date", ""))
        pk = int(request.POST.get("item", ""))
    except ValueError:
        messages.error(request, "Choose a day to move the idea to.")
        return _back(request)
    item = ContentPlanItem.objects.filter(account=account, pk=pk).first()
    if item is None or item.plan_status == ContentPlanItem.PlanStatus.PUBLISHED:
        messages.error(request, "That idea can't be moved.")
    elif day < timezone.localdate():
        messages.error(request, "Choose today or a later day.")
    else:
        item.planned_date = day
        item.save(update_fields=["planned_date"])
        messages.success(request, f"Idea moved to {date_format(day, 'l, N j')}.")
    return _back(request)


@login_required
@module_required("content_studio")
@_in_workspace_tz
def slots_manage(request):
    """The weekly posting slots, and when this business's posts have done best."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    can_admin = is_account_admin(request.user, account)
    if request.method == "POST":
        if not can_admin:
            messages.error(request, "Only an Owner or Admin can change the schedule.")
        elif request.POST.get("action") == "delete":
            PostingSlot.objects.filter(
                account=account, pk=request.POST.get("slot")
            ).delete()
            messages.success(request, "Slot removed.")
        else:
            try:
                days = request.POST.getlist("weekday")
                if not days:
                    raise scheduling.SchedulingError("Choose at least one day.")
                added = 0
                for weekday in days:
                    try:
                        scheduling.add_slot(account, weekday, request.POST.get("time"))
                        added += 1
                    except scheduling.SchedulingError:
                        if len(days) == 1:
                            raise
                messages.success(
                    request, f"Added {added} slot{'s' if added != 1 else ''}."
                )
            except scheduling.SchedulingError as exc:
                messages.error(request, str(exc))
        return redirect("publishing:slots")

    slots = list(PostingSlot.objects.filter(account=account))
    week = [
        {"day": name, "slots": [s for s in slots if s.weekday == i]}
        for i, name in enumerate(scheduling.DAY_NAMES)
    ]
    from .results import publishing_channels

    best = [
        scheduling.best_times(account, platform)
        for platform in (
            ContentPostVariant.Platform.INSTAGRAM,
            ContentPostVariant.Platform.FACEBOOK,
        )
        if platform in publishing_channels(account)
    ]
    return overlay_or_page(
        request,
        "publishing/_slots.html",
        "publishing/slots.html",
        {
            "account": account,
            "week": week,
            "has_slots": bool(slots),
            "day_names": list(enumerate(scheduling.DAY_NAMES)),
            "best": best,
            "best_min_per_time": scheduling.BEST_MIN_PER_TIME,
            "can_admin": can_admin,
            "current_tz_name": timezone.get_current_timezone_name(),
            "next_free": scheduling.next_free_slot(account, ["instagram", "facebook"]),
        },
        size="lg",
        title="Posting slots",
    )


@login_required
@module_required("content_studio")
@_in_workspace_tz
def slot_next(request):
    """The composer's "Next free slot": ``{at}`` as a local "YYYY-MM-DDTHH:MM", or
    ``{at: null, reason}``."""
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "Not found"}, status=404)
    platforms = [
        p
        for p in request.GET.getlist("platforms")
        if p in ContentPostVariant.Platform.values
    ]
    if not platforms:
        return JsonResponse({"at": None, "reason": "Choose a channel first."})
    if not PostingSlot.objects.filter(account=account).exists():
        return JsonResponse(
            {"at": None, "reason": "No posting slots yet.", "setup": True}
        )
    at = scheduling.next_free_slot(account, platforms)
    if at is None:
        return JsonResponse(
            {
                "at": None,
                "reason": f"Every slot in the next {scheduling.SLOT_WEEKS} weeks "
                "is taken.",
            }
        )
    return JsonResponse({"at": timezone.localtime(at).strftime("%Y-%m-%dT%H:%M")})
