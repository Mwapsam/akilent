"""The Comments view: Instagram and Facebook comments in one place (``comments``)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account, viewing_as
from apps.conversations import comments


@login_required
def comments_inbox(request):
    """Recent comments on the business's posts, grouped by post, with actions."""
    import uuid

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    channels = comments.usable_channels(account)
    channel = request.GET.get("channel", "")
    if channel not in channels:
        channel = ""
    filter_by = request.GET.get("filter", "all")
    if filter_by not in comments.FILTERS:
        filter_by = "all"
    post_id = request.GET.get("post", "")[:100]
    variant = request.GET.get("variant", "")
    variant_id = int(variant) if variant.isdigit() else None
    posts = (
        comments.posts_with_comments(
            account,
            channel=channel,
            filter_by=filter_by,
            post_id=post_id,
            variant_id=variant_id,
        )
        if channels
        else []
    )
    return render(
        request,
        "conversations/comments.html",
        {
            "account": account,
            "channels": channels,
            "channel": channel,
            "filter_by": filter_by,
            "post_id": post_id or variant,
            "posts": posts,
            "client_key": uuid.uuid4().hex,
            "read_only": viewing_as(request) is not None,
        },
    )


def _back(request):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("conversations:comments")


def _guard(request):
    account = get_current_account(request)
    if account is None:
        return None, redirect("dashboard")
    if viewing_as(request) is not None:
        messages.error(
            request, "You're viewing as this business, so you can't act here."
        )
        return None, _back(request)
    return account, None


@login_required
@require_POST
def comment_on_post(request):
    account, response = _guard(request)
    if response:
        return response
    try:
        comments.post_comment(
            account,
            request.POST.get("channel", ""),
            request.POST.get("post_id", ""),
            request.POST.get("body", ""),
            request.user,
            request.POST.get("client_key", ""),
        )
        messages.success(request, "Comment posted.")
    except comments.CommentError as exc:
        messages.error(request, str(exc))
    return _back(request)


@login_required
@require_POST
def reply(request, channel: str, pk: int):
    account, response = _guard(request)
    if response:
        return response
    try:
        comments.reply(
            account,
            channel,
            pk,
            request.POST.get("body", ""),
            request.user,
            request.POST.get("client_key", ""),
        )
        messages.success(request, "Reply posted.")
    except comments.CommentError as exc:
        messages.error(request, str(exc))
    return _back(request)


@login_required
@require_POST
def private_reply(request, channel: str, pk: int):
    account, response = _guard(request)
    if response:
        return response
    try:
        conversation = comments.private_reply(
            account,
            channel,
            pk,
            request.POST.get("body", ""),
            request.user,
            request.POST.get("client_key", ""),
        )
    except comments.CommentError as exc:
        messages.error(request, str(exc))
        return _back(request)
    if conversation is not None:
        messages.success(
            request, "Message sent. The conversation is now in your inbox."
        )
        return redirect("conversations:detail", public_id=conversation.public_id)
    messages.info(request, "Message queued — it will be sent shortly.")
    return _back(request)


_DONE = {
    "hide": "Comment hidden.",
    "unhide": "Comment visible again.",
    "delete": "Comment deleted.",
    "approve": "Flag cleared.",
}


@login_required
@require_POST
def moderate(request, channel: str, pk: int):
    account, response = _guard(request)
    if response:
        return response
    action = request.POST.get("action", "")
    try:
        comments.moderate(account, channel, pk, action, request.user)
        messages.success(request, _DONE.get(action, "Done."))
    except comments.CommentError as exc:
        messages.error(request, str(exc))
    return _back(request)
