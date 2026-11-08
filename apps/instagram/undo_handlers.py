"""Undo for comment rules (registered in apps/core/undo.py)."""

from __future__ import annotations

from apps.core.undo import UndoError


def rule_pause(request, account, pk, extra) -> str:
    """Switch a comment rule back on after it was just paused."""
    from apps.instagram.models import CommentTrigger, ModerationRule

    model = CommentTrigger if extra.get("kind") == "reply" else ModerationRule
    rule = model.objects.filter(account=account, pk=pk).first()
    if rule is None:
        raise UndoError("That rule is gone.")
    if not rule.is_active:
        rule.is_active = True
        rule.save(update_fields=["is_active", "updated_at"])
    return f'Rule "{rule.name}" switched on again.'
