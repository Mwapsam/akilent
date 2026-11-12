"""The post page's "Auto-reply to comments" card (apps.publishing.comment_replies)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.core.module_gate import module_required

from . import comment_replies
from .models import ContentPost


def _post_and_rule(request, pk, rule_pk=None):
    if not comment_replies.is_released():
        raise Http404
    account = get_current_account(request)
    if account is None:
        raise Http404
    post = get_object_or_404(ContentPost, pk=pk, account=account)
    rule = (
        get_object_or_404(comment_replies.rules_for(post), pk=rule_pk)
        if rule_pk is not None
        else None
    )
    return account, post, rule


def _back(post):
    return redirect(reverse("publishing:post-detail", args=[post.pk]) + "#pd-replies")


@login_required
@module_required("content_studio")
@require_POST
def comment_reply_save(request, pk, rule_pk=None):
    account, post, rule = _post_and_rule(request, pk, rule_pk)
    result = comment_replies.save(post, account, request.POST, rule=rule)
    if result.errors:
        for message in result.errors.values():
            messages.error(request, message)
    elif result.note:
        messages.warning(request, result.note)
    else:
        messages.success(
            request, "Saved. Comments on this post now get your private message."
        )
    return _back(post)


@login_required
@module_required("content_studio")
@require_POST
def comment_reply_toggle(request, pk, rule_pk):
    _account, post, rule = _post_and_rule(request, pk, rule_pk)
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request, "Auto-reply is on." if rule.is_active else "Auto-reply is paused."
    )
    return _back(post)


@login_required
@module_required("content_studio")
@require_POST
def comment_reply_delete(request, pk, rule_pk):
    _account, post, rule = _post_and_rule(request, pk, rule_pk)
    rule.delete()
    messages.success(request, "Auto-reply removed.")
    return _back(post)
