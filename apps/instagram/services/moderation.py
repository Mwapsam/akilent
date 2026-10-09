"""
Moderation service for Instagram comments.

Responsibilities (deliberately separated):
  1. evaluate_rules()  — determine which rule matches a comment
  2. execute_moderation() — apply the moderation action to the comment via provider
  3. fire_automation()   — trigger downstream automation (staff notify / proposal)
  4. moderate_comment()  — orchestrates 1-3 and writes ModerationLog

Design invariant (from Phase 2 spec):
  Moderation actions (hide, delete, flag) affect Instagram content.
  Automation triggers (notify_staff, create_proposal → opens a lead) affect Akilent state.
  They are separate concerns and evaluated independently.
"""

from __future__ import annotations

import logging
import re

from apps.instagram.models.comment import Comment
from apps.instagram.models.moderation import ModerationLog, ModerationRule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def moderate_comment(comment: Comment) -> ModerationLog | None:
    """
    Evaluate all active rules for the comment's account, apply the first
    matching rule's moderation action, fire its automation trigger, and
    return an immutable ModerationLog (or None if no rule matched).
    """
    if comment.moderation_logs.exists():
        # Already moderated: a redelivered or reprocessed webhook must not act,
        # notify or open a lead a second time.
        return None

    account = comment.thread.instagram_account.account
    rules = ModerationRule.objects.filter(account=account, is_active=True).order_by(
        "priority", "created_at"
    )

    matched_rule = None
    for rule in rules:
        if _rule_matches(rule, comment):
            matched_rule = rule
            break

    if matched_rule is None:
        return None

    moderation_action = matched_rule.moderation_action
    automation_trigger = matched_rule.automation_trigger

    outcome, error = _execute_moderation(comment, moderation_action)
    _fire_automation(comment, automation_trigger)
    if (
        moderation_action == ModerationRule.ModerationAction.FLAG
        or automation_trigger == ModerationRule.AutomationTrigger.NOTIFY_STAFF
    ):
        _notify_staff(comment, matched_rule, outcome, error)

    log = ModerationLog(
        account=account,
        rule=matched_rule,
        comment=comment,
        moderation_action=moderation_action,
        automation_trigger=automation_trigger,
        outcome=outcome,
        error=error,
    )
    log.save()
    return log


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------

_SPAM_PATTERNS = [
    r"\bfollow\s+back\b",
    r"\bcheck\s+(out\s+)?my\s+(profile|page|bio)\b",
    r"\b(dm|message)\s+me\s+for\b",
    r"\bclick\s+(the\s+)?link\b",
    r"(?:\S+\s+){0,3}(?:http|https|www\.)\S+",  # external link
]

_TOXICITY_PATTERNS = [
    r"\b(hate|kill|die|idiot|stupid|loser|trash|garbage)\b",
]

_COMPLAINT_PATTERNS = [
    r"\b(refund|scam|fraud|fake|disappointed|unacceptable|worst|terrible|horrible)\b",
]

_BUYING_INTENT_PATTERNS = [
    r"\b(buy|purchase|order|price|how\s+much|available|in\s+stock)\b",
]

_MENTION_PATTERNS = [
    r"@\w+",
]

_COMPILED: dict[str, list[re.Pattern]] = {
    ModerationRule.MatchType.SPAM_DETECTION: [
        re.compile(p, re.IGNORECASE) for p in _SPAM_PATTERNS
    ],
    ModerationRule.MatchType.TOXICITY: [
        re.compile(p, re.IGNORECASE) for p in _TOXICITY_PATTERNS
    ],
    ModerationRule.MatchType.COMPLAINT: [
        re.compile(p, re.IGNORECASE) for p in _COMPLAINT_PATTERNS
    ],
    ModerationRule.MatchType.BUYING_INTENT: [
        re.compile(p, re.IGNORECASE) for p in _BUYING_INTENT_PATTERNS
    ],
    ModerationRule.MatchType.MENTION: [
        re.compile(p, re.IGNORECASE) for p in _MENTION_PATTERNS
    ],
}


def _rule_matches(rule: ModerationRule, comment: Comment) -> bool:
    body = comment.body or ""

    # Keywords count on every rule type: on a heuristic rule they add to the
    # built-in patterns. Ignoring them there silently broke rules businesses
    # wrote as "Spam detection" + their own spam phrases.
    body_lower = body.lower()
    if any(kw in body_lower for kw in rule.keyword_list()):
        return True

    patterns = _COMPILED.get(rule.match_type, [])
    return any(p.search(body) for p in patterns)


# ---------------------------------------------------------------------------
# Moderation execution
# ---------------------------------------------------------------------------


def _execute_moderation(comment: Comment, action: str) -> tuple[str, str]:
    """
    Apply the moderation action to the comment on Instagram and update the
    local Comment record. Returns (outcome, error_message).
    """
    if action == ModerationRule.ModerationAction.NONE:
        return ModerationLog.Outcome.SKIPPED, ""

    if action == ModerationRule.ModerationAction.FLAG:
        # FLAG is a local-only action — no API call; the team is emailed.
        comment.moderation_state = Comment.ModerationState.FLAGGED
        comment.save(update_fields=["moderation_state"])
        return ModerationLog.Outcome.APPLIED, ""

    if comment.moderation_state in {
        Comment.ModerationState.HIDDEN,
        Comment.ModerationState.DELETED,
    }:
        return ModerationLog.Outcome.SKIPPED, ""

    from apps.instagram.providers.meta import MetaInstagramProvider

    account = comment.thread.instagram_account
    if not account.access_token:
        return (
            ModerationLog.Outcome.FAILED,
            "The Instagram account has no access token — reconnect it.",
        )
    provider = MetaInstagramProvider(
        access_token=account.access_token,
        instagram_account_id=account.instagram_business_account_id,
    )

    if action == ModerationRule.ModerationAction.HIDE:
        success, error = provider.hide_comment(comment.comment_id)
        if success:
            comment.is_hidden = True
            comment.moderation_state = Comment.ModerationState.HIDDEN
            comment.save(update_fields=["is_hidden", "moderation_state"])
            return ModerationLog.Outcome.APPLIED, ""
        return ModerationLog.Outcome.FAILED, f"Instagram refused to hide it: {error}"

    if action == ModerationRule.ModerationAction.DELETE:
        success, error = provider.delete_comment(comment.comment_id)
        if success:
            comment.moderation_state = Comment.ModerationState.DELETED
            comment.save(update_fields=["moderation_state"])
            return ModerationLog.Outcome.APPLIED, ""
        return ModerationLog.Outcome.FAILED, f"Instagram refused to delete it: {error}"

    return ModerationLog.Outcome.SKIPPED, f"unknown action: {action}"


# ---------------------------------------------------------------------------
# Automation triggers
# ---------------------------------------------------------------------------


def _fire_automation(comment: Comment, trigger: str) -> None:
    """
    Fire the automation trigger. These affect Akilent state only — they do not
    touch Instagram content.
    """
    # NOTIFY_STAFF is sent by moderate_comment, which knows the action's outcome.
    if trigger == ModerationRule.AutomationTrigger.CREATE_PROPOSAL:
        _open_lead(comment)


_ACTION_DONE: dict[str, str] = {
    ModerationRule.ModerationAction.HIDE: "It was hidden.",
    ModerationRule.ModerationAction.DELETE: "It was deleted.",
    ModerationRule.ModerationAction.FLAG: "It was flagged for review.",
}


def _notify_staff(
    comment: Comment, rule: ModerationRule, outcome: str, error: str
) -> None:
    """Email the business's owners about the comment. Never breaks inbound processing."""
    from django.urls import reverse

    from apps.accounts.notifications import notify_team

    account = comment.thread.instagram_account.account
    who = comment.instagram_contact.username or comment.instagram_contact.name
    lines = [
        f"{'@' + who if who else 'Someone'} commented on your Instagram post:",
        f"“{(comment.body or '')[:300]}”",
        f"It matched your moderation rule “{rule.name}”.",
    ]
    if outcome == ModerationLog.Outcome.APPLIED:
        lines.append(_ACTION_DONE.get(rule.moderation_action, ""))
    elif outcome == ModerationLog.Outcome.FAILED:
        lines.append(f"Akilent could not act on it. {error}")
    try:
        sent = notify_team(
            account,
            to="owners",
            subject="An Instagram comment needs a look",
            text="\n\n".join(line for line in lines if line),
            path=reverse("instagram-comment-rules"),
        )
        logger.info(
            "Moderation notice for comment %s emailed to %s teammate(s) on account %s",
            comment.comment_id,
            sent,
            account.pk,
        )
    except Exception:
        logger.exception(
            "Moderation notice failed for comment %s on account %s",
            comment.comment_id,
            account.pk,
        )


def _open_lead(comment: Comment) -> None:
    """Open a lead for the commenter — the same path a buying-intent DM takes.

    Goes through the action registry, so the "Track potential sales" module
    switch and CRM's one-open-lead-per-customer rule apply. A comment has no
    conversation yet; the lead links to the customer, and the DM a private reply
    opens later lands on the same customer.
    """
    from apps.core.actions import ActionError, run_action

    thread = comment.thread
    account = thread.instagram_account.account
    ig_contact = thread.instagram_contact
    if not ig_contact.contact_id:
        logger.debug(
            "_open_lead: no canonical contact for ig_contact %s", ig_contact.pk
        )
        return
    try:
        run_action(
            "capture_conversation_lead",
            {"account": account},
            account=account,
            contact=ig_contact.contact,
            conversation_id=thread.conversation.public_id
            if thread.conversation
            else "",
            signal="instagram comment",
        )
    except ActionError as exc:
        logger.info(
            "_open_lead: not opened for comment %s: %s", comment.comment_id, exc
        )
    except Exception:
        logger.exception("_open_lead failed for comment %s", comment.comment_id)
