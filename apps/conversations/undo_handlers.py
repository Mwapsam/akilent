"""Undo for the inbox's small actions (registered in apps/core/undo.py).

Each takes ``(request, account, pk, extra)``, undoes for real, and returns the toast's
words; ``UndoError`` when it can't be undone now.
"""

from __future__ import annotations

from apps.core.undo import UndoError


def followup_done(request, account, pk, extra) -> str:
    """Reopen a follow-up that was just marked done."""
    from apps.conversations import followup_copy
    from apps.conversations.actions import ActionError, run_action
    from apps.conversations.models import FollowUp

    followup = FollowUp.objects.filter(account=account, pk=pk).first()
    if followup is None:
        raise UndoError("That follow-up is gone.")
    try:
        run_action("reopen_followup", {"account": account}, followup=followup)
    except ActionError as exc:
        raise UndoError(str(exc)) from exc
    return followup_copy.REOPENED


def comment_hide(request, account, pk, extra) -> str:
    """Unhide a comment that was just hidden: on Instagram/Facebook, not only here."""
    from apps.conversations import comments

    try:
        comments.moderate(account, extra.get("channel", ""), pk, "unhide", request.user)
    except comments.CommentError as exc:
        raise UndoError(f"Couldn't unhide it: {exc}") from exc
    return "Comment visible again."


def comment_hide_many(request, account, pk, extra) -> str:
    """Unhide the comments a bulk Hide just hid, each on Instagram/Facebook."""
    from apps.conversations import comments

    items = []
    for raw in extra.get("items") or []:
        channel, _, comment_pk = str(raw).partition(":")
        if comment_pk.isdigit():
            items.append((channel, int(comment_pk)))
    done, failed, error = comments.moderate_many(account, items, "unhide", request.user)
    if not done:
        raise UndoError(f"Couldn't unhide them: {error}")
    n = len(done)
    words = f"{n} comment{'' if n == 1 else 's'} visible again."
    if failed:
        words += f" {len(failed)} still hidden: {error}"
    return words


def saved_reply_delete(request, account, pk, extra) -> str:
    """Bring back a saved reply that was just deleted."""
    from apps.conversations.models import SavedReply

    restored = SavedReply.all_objects.filter(
        account=account, pk=pk, deleted_at__isnull=False
    ).update(deleted_at=None)
    if not restored:
        raise UndoError("That saved reply can't be brought back.")
    return "Saved reply restored."
