"""Recurring posts (apps.publishing.recurring). Behind the ``cs_recurring`` release
gate: until it is released, ReleaseGateMiddleware 404s these URLs."""

from __future__ import annotations

import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.core.module_gate import module_required

from . import recurring
from .composer import parse_schedule
from .models import ContentPost, RecurrenceRule
from .views import _in_workspace_tz

# Alternate captions are separated by a line of three dashes, so one can span lines.
_CAPTION_SPLIT = re.compile(r"^\s*---\s*$", re.MULTILINE)


@login_required
@module_required("content_studio")
@_in_workspace_tz
def recurring_list(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    rules = list(
        RecurrenceRule.objects.filter(account=account)
        .select_related("source")
        .prefetch_related("runs__post")
    )
    for rule in rules:
        rule.recent_runs = list(rule.runs.all()[:5])  # type: ignore[attr-defined]
    return render(
        request, "publishing/recurring.html", {"account": account, "rules": rules}
    )


@login_required
@module_required("content_studio")
@require_POST
@_in_workspace_tz
def recurring_create(request, pk):
    """Repeat a post (from its page)."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    post = get_object_or_404(ContentPost, pk=pk, account=account)
    captions = _CAPTION_SPLIT.split(request.POST.get("captions", ""))
    try:
        rule = recurring.create_rule(
            post,
            request.user,
            request.POST.get("every_days"),
            parse_schedule(request.POST.get("first_run_at", "")),
            captions,
        )
    except recurring.RecurrenceError as exc:
        messages.error(request, str(exc))
        return redirect("publishing:post-detail", pk=post.pk)
    messages.success(
        request,
        f"This post will repeat every {rule.every_days} days. Each copy is checked "
        "before it's scheduled.",
    )
    return redirect("publishing:recurring")


@login_required
@module_required("content_studio")
@require_POST
def recurring_update(request, pk):
    """Pause, resume or delete a repeat."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    rule = get_object_or_404(RecurrenceRule, pk=pk, account=account)
    action = request.POST.get("action")
    if action == "delete":
        rule.delete()
        messages.success(request, "Repeat deleted. Copies already made are kept.")
    elif action in ("pause", "resume"):
        rule.active = action == "resume"
        if rule.active:
            # Times that passed while paused are skipped, not made late.
            from django.utils import timezone

            while rule.next_run_at <= timezone.now() + recurring.LEAD:
                rule.next_run_at = recurring._next_time(rule, rule.next_run_at)
        rule.save(update_fields=["active", "next_run_at"])
        messages.success(
            request, "Repeat resumed." if rule.active else "Repeat paused."
        )
    return redirect("publishing:recurring")
