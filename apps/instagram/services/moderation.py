"""
Moderation service for Instagram comments.

Responsibilities (deliberately separated):
  1. evaluate_rules()  — determine which rule matches a comment
  2. execute_moderation() — apply the moderation action to the comment via provider
  3. fire_automation()   — trigger downstream automation (staff notify / proposal)
  4. moderate_comment()  — orchestrates 1-3 and writes ModerationLog

Design invariant (from Phase 2 spec):
  Moderation actions (hide, delete, flag) affect Instagram content.
  Automation triggers (notify_staff, create_proposal) affect Akilent state.
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
    match_type = rule.match_type

    if match_type == ModerationRule.MatchType.KEYWORD:
        body_lower = body.lower()
        return any(kw in body_lower for kw in rule.keyword_list())

    patterns = _COMPILED.get(match_type, [])
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
        # FLAG is a local-only action — no API call; update moderation_state.
        comment.moderation_state = Comment.ModerationState.PENDING
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
        return ModerationLog.Outcome.SKIPPED, "no access token"
    provider = MetaInstagramProvider(
        access_token=account.access_token,
        instagram_account_id=account.instagram_business_account_id,
    )

    if action == ModerationRule.ModerationAction.HIDE:
        success = provider.hide_comment(comment.comment_id)
        if success:
            comment.is_hidden = True
            comment.moderation_state = Comment.ModerationState.HIDDEN
            comment.save(update_fields=["is_hidden", "moderation_state"])
            return ModerationLog.Outcome.APPLIED, ""
        return ModerationLog.Outcome.FAILED, "hide_comment API call failed"

    if action == ModerationRule.ModerationAction.DELETE:
        success = provider.delete_comment(comment.comment_id)
        if success:
            comment.moderation_state = Comment.ModerationState.DELETED
            comment.save(update_fields=["moderation_state"])
            return ModerationLog.Outcome.APPLIED, ""
        return ModerationLog.Outcome.FAILED, "delete_comment API call failed"

    return ModerationLog.Outcome.SKIPPED, f"unknown action: {action}"


# ---------------------------------------------------------------------------
# Automation triggers
# ---------------------------------------------------------------------------


def _fire_automation(comment: Comment, trigger: str) -> None:
    """
    Fire the automation trigger. These affect Akilent state only — they do not
    touch Instagram content.
    """
    if trigger == ModerationRule.AutomationTrigger.NONE:
        return

    if trigger == ModerationRule.AutomationTrigger.NOTIFY_STAFF:
        _notify_staff(comment)
    elif trigger == ModerationRule.AutomationTrigger.CREATE_PROPOSAL:
        _create_proposal(comment)


def _notify_staff(comment: Comment) -> None:
    """
    Log a staff notification for the comment. Actual notification delivery
    (email, in-app alert) is wired up by the notifications app in a separate
    phase; this layer emits the intent.
    """
    logger.info(
        "Staff notification triggered for comment %s on account %s",
        comment.comment_id,
        comment.thread.instagram_account.account_id,
    )


def _create_proposal(comment: Comment) -> None:
    """
    Create an AIProposal for buying intent detected in a comment. Only creates
    one if no pending proposal already exists for this comment thread.

    Comments don't have a conversation yet (that only exists after a private
    reply triggers a DM). The proposal is stored with conversation=None and
    the comment context in payload; staff review will link the eventual DM.
    """
    from apps.ai.models import AIProposal

    thread = comment.thread
    account = thread.instagram_account.account
    ig_contact = thread.instagram_contact

    # Skip if there is no canonical Contact: the proposal needs an account anchor
    # but conversation is optional. We use the thread's comment_id as dedup key.
    if not ig_contact.contact_id:
        logger.debug(
            "_create_proposal: no canonical contact for ig_contact %s", ig_contact.pk
        )
        return

    existing = AIProposal.objects.filter(
        account=account,
        action="purchase_intent",
        status=AIProposal.Status.PENDING,
        payload__comment_thread_id=thread.pk,
    ).exists()
    if existing:
        logger.debug(
            "_create_proposal: pending proposal already exists for thread %s", thread.pk
        )
        return

    AIProposal.objects.create(
        account=account,
        conversation=None,
        action="purchase_intent",
        confidence=0.8,
        payload={
            "source": "instagram_comment",
            "comment_id": comment.comment_id,
            "comment_thread_id": thread.pk,
            "igsid": ig_contact.instagram_scoped_id,
        },
    )
    logger.info(
        "AIProposal created for comment %s on account %s",
        comment.comment_id,
        account.pk,
    )
