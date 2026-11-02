"""Content Studio AI tools (apps.publishing.ai_plus). Behind the ``cs_ai_plus`` release
gate: until it is released, ReleaseGateMiddleware 404s every URL under ``publishing/ai/``."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.api import is_account_admin
from apps.accounts.utils import get_current_account
from apps.ai import api as ai_api
from apps.core.module_gate import module_required

from . import ai_plus

_TOOL_KINDS = ("content_rewrite", "content_hashtags", "content_ideas")


@login_required
@module_required("content_studio")
def ai_tools(request):
    """Brand voice, and post ideas from the posts that sold."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    can_admin = is_account_admin(request.user, account)
    if request.method == "POST":
        if not can_admin:
            messages.error(
                request, "Only an Owner or Admin can change the brand voice."
            )
        else:
            ai_plus.save_voice(account, request.POST.get("brand_voice", ""))
            messages.success(request, "Brand voice saved.")
        return redirect("publishing:ai-tools")
    sources = ai_plus.idea_sources(account)
    return render(
        request,
        "publishing/ai_tools.html",
        {
            "can_admin": can_admin,
            "brand_voice": ai_plus.voice(account),
            "max_voice": ai_plus.MAX_VOICE,
            "off_reason": ai_plus.off_reason(account),
            "sources": sources,
            "idea_days": ai_plus.IDEA_DAYS,
            "draft": _tool_draft(account, request.GET.get("ideas"))
            if request.GET.get("ideas")
            else None,
        },
    )


def _tool_draft(account, draft_id):
    draft = ai_api.get_draft(account, draft_id)
    return draft if draft is not None and draft.kind in _TOOL_KINDS else None


def _ideas_url(draft_id) -> str:
    return reverse("publishing:ai-tools") + f"?ideas={draft_id}#ideas"


def _json_error(message: str, status: int = 400):
    return JsonResponse({"error": message}, status=status)


@login_required
@module_required("content_studio")
@require_POST
def rewrite(request):
    account = get_current_account(request)
    if account is None:
        return _json_error("No business selected.", 403)
    try:
        draft = ai_plus.request_rewrite(
            account,
            request.user,
            request.POST.get("caption", ""),
            request.POST.get("instruction", ""),
            request.POST.get("platform", "instagram"),
        )
    except ai_plus.AiPlusError as exc:
        return _json_error(str(exc))
    return JsonResponse({"draft_id": draft.pk})


@login_required
@module_required("content_studio")
@require_POST
def hashtags(request):
    account = get_current_account(request)
    if account is None:
        return _json_error("No business selected.", 403)
    try:
        draft = ai_plus.request_hashtags(
            account,
            request.user,
            request.POST.get("caption", ""),
            request.POST.get("existing", "").split(),
            request.POST.get("platform", "instagram"),
        )
    except ai_plus.AiPlusError as exc:
        return _json_error(str(exc))
    return JsonResponse({"draft_id": draft.pk})


@login_required
@module_required("content_studio")
def result(request, draft_id):
    account = get_current_account(request)
    draft = _tool_draft(account, draft_id) if account else None
    if draft is None:
        return _json_error("Not found", 404)
    return JsonResponse(ai_api.draft_json(draft))


@login_required
@module_required("content_studio")
@require_POST
def ideas(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        draft = ai_plus.request_ideas(account, request.user)
    except ai_plus.AiPlusError as exc:
        messages.error(request, str(exc))
        return redirect("publishing:ai-tools")
    return redirect(_ideas_url(draft.pk))


@login_required
@module_required("content_studio")
def ideas_status(request, draft_id):
    """The ideas panel, polled by the page until the draft is finished."""
    account = get_current_account(request)
    draft = _tool_draft(account, draft_id) if account else None
    return render(request, "publishing/_ai_ideas.html", {"draft": draft})


@login_required
@module_required("content_studio")
@require_POST
def idea_save(request, draft_id):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        index = int(request.POST.get("index", ""))
    except ValueError:
        index = -1
    item = ai_plus.save_idea(account, draft_id, index)
    if item is None:
        messages.error(request, "That idea isn't available any more.")
    else:
        messages.success(request, f'Added "{item.topic}" to Planning as an idea.')
    return redirect(_ideas_url(draft_id))
