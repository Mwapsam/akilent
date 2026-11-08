"""One Undo for the small actions people take by mistake: a toast's Undo button POSTs here.

``undo_url(request, action, pk)`` signs what was done, by whom, in which workspace; the
toast (``accounts.utils.flash`` / ``core.htmx.action_done`` with ``undo=``) offers it for 30
seconds. The view checks the signature, the person and the workspace, then runs the
action's handler, which undoes it for real on the server (unhiding a comment calls Meta).

Handlers live with their app and are named here as dotted paths (no import cycles):

* ``followup_done``: reopen a follow-up marked done;
* ``rule_pause``: switch a comment rule back on;
* ``comment_hide``: unhide a comment on Instagram/Facebook;
* ``saved_reply_delete``: bring back a deleted saved reply.

A handler takes ``(request, account, pk, extra)`` and returns the toast's words, or raises
``UndoError`` with words for why it can't be undone now.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import Http404
from django.urls import reverse
from django.utils.module_loading import import_string
from django.views.decorators.http import require_POST

SALT = "akilent.undo"
# The toast offers it for 30 seconds; a little longer here for a slow network.
MAX_AGE = 90

HANDLERS = {
    "followup_done": "apps.conversations.undo_handlers.followup_done",
    "rule_pause": "apps.instagram.undo_handlers.rule_pause",
    "comment_hide": "apps.conversations.undo_handlers.comment_hide",
    "saved_reply_delete": "apps.conversations.undo_handlers.saved_reply_delete",
}


class UndoError(Exception):
    """It can't be undone now; the message says why, for the toast."""


def undo_url(request, action: str, pk, **extra) -> str:
    """The signed path a toast's Undo POSTs to."""
    from apps.accounts.utils import get_current_account

    if action not in HANDLERS:
        raise ValueError(f"No undo handler for {action!r}")
    account = get_current_account(request)
    token = signing.dumps(
        {
            "a": action,
            "p": pk,
            "u": request.user.pk,
            "w": account.pk if account else None,
            "x": extra,
        },
        salt=SALT,
        compress=True,
    )
    return reverse("undo") + "?t=" + token


@login_required
@require_POST
def undo(request):
    from apps.accounts.utils import get_current_account
    from apps.core.htmx import action_done

    account = get_current_account(request)
    if account is None:
        raise Http404
    try:
        data = signing.loads(request.GET.get("t", ""), salt=SALT, max_age=MAX_AGE)
    except signing.BadSignature:
        data = None
    back = request.headers.get("Referer") or "/dashboard/"
    if (
        not data
        or data.get("u") != request.user.pk
        or data.get("w") != account.pk
        or data.get("a") not in HANDLERS
    ):
        return action_done(
            request, "That can't be undone any more.", back, level="warning"
        )
    handler = import_string(HANDLERS[data["a"]])
    try:
        message = handler(request, account, data["p"], data.get("x") or {})
    except UndoError as exc:
        return action_done(request, str(exc), back, level="warning")
    return action_done(request, message, back)
