"""Moderation for Facebook Page comments, with the account's shared moderation rules.

Rule matching is shared with Instagram (``apps.instagram.services.moderation``); this
module only applies the outcome on Facebook and writes the same immutable
ModerationLog.
"""

from __future__ import annotations

import logging

from apps.facebook.models.comment import FacebookComment
from apps.instagram.models.moderation import ModerationLog, ModerationRule

logger = logging.getLogger(__name__)


def moderate_comment(comment: FacebookComment, conn) -> ModerationLog | None:
    """Apply the first matching rule's action and automation; log it. Once per comment."""
    from apps.instagram.services.moderation import first_matching_rule

    if comment.moderation_logs.exists():
        return None  # a redelivered webhook must not act or notify twice

    account = conn.account
    rule = first_matching_rule(account, "facebook", comment.body or "")
    if rule is None:
        return None

    outcome, error = execute_moderation(comment, rule.moderation_action, conn)
    if rule.automation_trigger == ModerationRule.AutomationTrigger.CREATE_PROPOSAL:
        _open_lead(comment)
    if (
        rule.moderation_action == ModerationRule.ModerationAction.FLAG
        or rule.automation_trigger == ModerationRule.AutomationTrigger.NOTIFY_STAFF
    ):
        _notify_staff(comment, rule, outcome, error)

    log = ModerationLog(
        account=account,
        rule=rule,
        facebook_comment=comment,
        moderation_action=rule.moderation_action,
        automation_trigger=rule.automation_trigger,
        outcome=outcome,
        error=error,
    )
    log.save()
    return log


def execute_moderation(comment: FacebookComment, action: str, conn) -> tuple[str, str]:
    """Apply ``action`` on Facebook and to the local record: ``(outcome, error)``."""
    from apps.facebook.providers.meta import provider_for

    A = ModerationRule.ModerationAction
    S = FacebookComment.ModerationState
    if action == A.NONE:
        return ModerationLog.Outcome.SKIPPED, ""
    if action == A.FLAG:
        comment.moderation_state = S.FLAGGED
        comment.save(update_fields=["moderation_state"])
        return ModerationLog.Outcome.APPLIED, ""
    if comment.moderation_state in {S.HIDDEN, S.DELETED}:
        return ModerationLog.Outcome.SKIPPED, ""
    if conn is None or not conn.access_token:
        return (
            ModerationLog.Outcome.FAILED,
            "The Facebook Page isn't connected — reconnect it.",
        )

    provider = provider_for(conn)
    if action == A.HIDE:
        ok, error = provider.hide_comment(comment.comment_id)
        if ok:
            comment.is_hidden = True
            comment.moderation_state = S.HIDDEN
            comment.save(update_fields=["is_hidden", "moderation_state"])
            return ModerationLog.Outcome.APPLIED, ""
        return ModerationLog.Outcome.FAILED, f"Facebook refused to hide it: {error}"
    if action == A.DELETE:
        ok, error = provider.delete_comment(comment.comment_id)
        if ok:
            comment.moderation_state = S.DELETED
            comment.save(update_fields=["moderation_state"])
            return ModerationLog.Outcome.APPLIED, ""
        return ModerationLog.Outcome.FAILED, f"Facebook refused to delete it: {error}"
    return ModerationLog.Outcome.SKIPPED, f"unknown action: {action}"


_ACTION_DONE = {
    ModerationRule.ModerationAction.HIDE: "It was hidden.",
    ModerationRule.ModerationAction.DELETE: "It was deleted.",
    ModerationRule.ModerationAction.FLAG: "It was flagged for review.",
}


def _notify_staff(comment: FacebookComment, rule, outcome: str, error: str) -> None:
    """Email the business's owners about the comment. Never breaks inbound processing."""
    from django.urls import reverse

    from apps.accounts.notifications import notify_team

    account = comment.thread.account
    who = comment.facebook_contact.name
    lines = [
        f"{who or 'Someone'} commented on your Facebook Page:",
        f"“{(comment.body or '')[:300]}”",
        f"It matched your moderation rule “{rule.name}”.",
    ]
    if outcome == ModerationLog.Outcome.APPLIED:
        lines.append(_ACTION_DONE.get(rule.moderation_action, ""))
    elif outcome == ModerationLog.Outcome.FAILED:
        lines.append(f"Akilent could not act on it. {error}")
    try:
        notify_team(
            account,
            to="owners",
            subject="A Facebook comment needs a look",
            text="\n\n".join(line for line in lines if line),
            path=reverse("conversations:comments") + "?channel=facebook&filter=flagged",
        )
    except Exception:
        logger.exception("Facebook moderation notice failed for %s", comment.comment_id)


def _open_lead(comment: FacebookComment) -> None:
    """Open a lead for the commenter, the same path an Instagram comment rule takes."""
    from apps.core.actions import ActionError, run_action

    thread = comment.thread
    fb_contact = thread.facebook_contact
    if not fb_contact.contact_id:
        return
    try:
        run_action(
            "capture_conversation_lead",
            {"account": thread.account},
            account=thread.account,
            contact=fb_contact.contact,
            conversation_id=thread.conversation.public_id
            if thread.conversation
            else "",
            signal="facebook comment",
        )
    except ActionError as exc:
        logger.info(
            "facebook _open_lead: not opened for %s: %s", comment.comment_id, exc
        )
    except Exception:
        logger.exception("facebook _open_lead failed for %s", comment.comment_id)
