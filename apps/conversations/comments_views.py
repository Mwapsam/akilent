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
    """The Comments tab: one list of comments on the business's posts, newest first, each
    with its post and its replies beneath. Not answered by default; "Load more" pages on
    (``?before=``, answered with just the next rows)."""
    import uuid

    from django.utils.dateparse import parse_datetime

    from apps.core.htmx import is_background, partial_response

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    channels = comments.usable_channels(account)
    channel = request.GET.get("channel", "")
    if channel not in channels:
        channel = ""
    post_id = request.GET.get("post", "")[:100]
    variant = request.GET.get("variant", "")
    variant_id = int(variant) if variant.isdigit() else None
    # One post (from its row or from Content Studio) shows all its comments by default.
    default = "all" if post_id or variant_id else "unanswered"
    filter_by = request.GET.get("filter", default)
    if filter_by not in comments.FEED_FILTERS:
        filter_by = default
    before = parse_datetime(request.GET.get("before", "") or "")
    from apps.audience.reports import TOPIC_FILTERS
    from apps.core.releases import is_released

    # What each comment is about, and the topic filter (spec: audience-intelligence-spec).
    show_topics = is_released("audience_insights")
    topic = request.GET.get("topic", "") if show_topics else ""
    if topic not in dict(TOPIC_FILTERS):
        topic = ""
    threads, cursor = (
        comments.comment_feed(
            account,
            channel=channel,
            filter_by=filter_by,
            post_id=post_id,
            variant_id=variant_id,
            before=before,
            topic=topic,
            with_topics=show_topics,
        )
        if channels
        else ([], None)
    )
    header = (
        comments.post_header(account, channel, post_id, variant_id)
        if channels and (post_id or variant_id)
        else None
    )
    query = request.GET.copy()
    query.pop("before", None)
    ctx = {
        "account": account,
        "channels": channels,
        "channel": channel,
        "filter_by": filter_by,
        "filters": FILTER_LABELS,
        "show_topics": show_topics,
        "topic": topic,
        "topic_filters": TOPIC_FILTERS if show_topics else (),
        "post_id": post_id or variant,
        "post": header[0] if header else None,
        "post_ref": header[1] if header else None,
        "threads": [t for t in threads if not t.collapsed],
        "collapsed": [t for t in threads if t.collapsed],
        "more_url": ("?" + _with(query, before=cursor.isoformat()) if cursor else ""),
        "client_key": uuid.uuid4().hex,
        "read_only": viewing_as(request) is not None,
        "next_url": "?" + query.urlencode() if query else request.path,
    }
    if before is not None and is_background(request):
        return partial_response(
            render(request, "conversations/_comment_items.html", ctx)
        )
    return render(request, "conversations/comments.html", ctx)


FILTER_LABELS = (
    ("unanswered", "Not answered"),
    ("all", "Everything"),
    ("intent", "Wants to buy"),
    ("flagged", "Flagged"),
)


def _with(query, **changes) -> str:
    q = query.copy()
    for key, value in changes.items():
        q[key] = value
    return q.urlencode()


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
        undo = None
        if action == "hide":
            from apps.core.undo import undo_url

            # The Undo unhides it on Instagram/Facebook too (undo_handlers.comment_hide).
            undo = undo_url(request, "comment_hide", pk, channel=channel)
        from apps.accounts.utils import flash

        flash(request, "success", _DONE.get(action, "Done."), undo=undo)
    except comments.CommentError as exc:
        messages.error(request, str(exc))
    return _back(request)


@login_required
@require_POST
def bulk(request):
    """Hide or permanently delete the selected comments (``items``: "channel:pk")."""
    account, response = _guard(request)
    if response:
        return response
    action = request.POST.get("action", "")
    items = []
    for raw in request.POST.getlist("items")[:200]:
        channel, _, pk = raw.partition(":")
        if channel in comments.CHANNELS and pk.isdigit():
            items.append((channel, int(pk)))
    if not items:
        messages.error(request, "Select at least one comment.")
        return _back(request)
    if action not in ("hide", "delete"):
        messages.error(request, "Unknown action.")
        return _back(request)
    from apps.accounts.utils import flash

    done, failed, error = comments.moderate_many(account, items, action, request.user)
    n = len(done)
    noun = f"{n} comment{'' if n == 1 else 's'}"
    if action == "hide":
        words = f"Hid {noun}."
    else:
        words = f"Deleted {noun}."
    if failed:
        words += f" {len(failed)} couldn't be {'hidden' if action == 'hide' else 'deleted'}: {error}"
    undo = None
    if action == "hide" and done:
        from apps.core.undo import undo_url

        # Undo unhides each one on Instagram/Facebook (undo_handlers.comment_hide_many).
        undo = undo_url(
            request, "comment_hide_many", 0, items=[f"{c}:{pk}" for c, pk in done]
        )
    flash(request, "warning" if failed else "success", words, undo=undo)
    return _back(request)


@login_required
@require_POST
def topics(request, channel: str, pk: int):
    """The owner corrects what a comment is about (``topics``: the ticked categories).
    The correction belongs to the comment and outlives reclassification (apps.audience)."""
    from apps.audience.services import set_topics
    from apps.core.releases import is_released

    account, response = _guard(request)
    if response:
        return response
    if not is_released("audience_insights"):
        return _back(request)
    try:
        comment = comments._comment(account, channel, pk)
    except comments.CommentError as exc:
        messages.error(request, str(exc))
        return _back(request)
    set_topics(comment, request.POST.getlist("topics"), user=request.user)
    messages.success(request, "Topics saved.")
    return _back(request)
