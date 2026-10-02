"""Policy execution engine.

Public surface
--------------
    evaluate_policies(account) -> dict
        Iterates all ACTIVE BusinessPolicy rows for the account, evaluates their
        triggers, dispatches actions via existing platform capabilities, and records
        a PolicyExecution audit row for each evaluation attempt.

        Returns {"executed": n, "skipped": n, "failed": n}.

Everything else in this module is private.

Execution semantics
-------------------
    SKIPPED   — trigger matched no targets, OR idempotency guard blocked re-dispatch
    COMPLETED — action was successfully invoked (campaign queued, follow-up created,
                notify_owner deferred deliberately)
    FAILED    — action raised an error (missing campaign_id, unexpected exception)

A COMPLETED execution is NOT a business outcome.  It records that Akilent
successfully invoked the configured action — it does not mean the customer
responded, purchased, or that the recommendation "worked."  Business outcome
measurement lives in RecommendationLog.outcome_* and is a separate P1 concern.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Known action types.  Activation guard in insights_views ensures only these
# reach ACTIVE policies, but the executor stays defensive.
_KNOWN_ACTION_TYPES = frozenset({"campaign", "create_followup", "notify_owner"})


class PolicyActionError(Exception):
    """Raised by action handlers for expected, non-retryable failures."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_policies(account) -> dict:
    """Evaluate all ACTIVE policies for *account*.

    Returns {"executed": n, "skipped": n, "failed": n}.
    """
    from apps.insights.models import BusinessPolicy

    policies = BusinessPolicy.objects.filter(
        account=account, status=BusinessPolicy.Status.ACTIVE
    )
    summary = {"executed": 0, "skipped": 0, "failed": 0}
    for policy in policies:
        result = _run_policy(policy, account)
        summary[result] += 1
    return summary


# ---------------------------------------------------------------------------
# Per-policy execution
# ---------------------------------------------------------------------------


def _run_policy(policy, account) -> str:
    """Evaluate and execute one policy.  Returns "executed" | "skipped" | "failed"."""
    from apps.insights.models import PolicyExecution

    targets = _check_trigger(policy, account)
    target_payload = _build_target(targets)

    # Idempotency: prevent re-dispatching the same action within the trigger window.
    if _already_executed(policy, target_payload):
        return "skipped"

    pe = PolicyExecution.objects.create(
        policy=policy,
        account=account,
        trigger_snapshot={"trigger": policy.trigger, "condition": policy.condition},
        target=target_payload,
        status=PolicyExecution.Status.STARTED,
    )

    if not targets:
        _mark_pe(pe, PolicyExecution.Status.SKIPPED)
        return "skipped"

    try:
        result = _execute_action(policy, targets, account)
        _mark_pe(pe, PolicyExecution.Status.COMPLETED, result=result)
        _attach_to_recommendation(policy, pe)
        return "executed"
    except PolicyActionError as exc:
        _mark_pe(pe, PolicyExecution.Status.FAILED, error=str(exc))
        return "failed"
    except Exception as exc:
        logger.exception(
            "executor: unexpected error for policy=%s account=%s",
            policy.pk,
            account.pk,
        )
        _mark_pe(pe, PolicyExecution.Status.FAILED, error=str(exc))
        return "failed"


# ---------------------------------------------------------------------------
# Trigger evaluators  (reuse existing state/rules logic — don't duplicate)
# ---------------------------------------------------------------------------


def _check_trigger(policy, account) -> list:
    """Return matching targets for the policy's trigger condition."""
    from apps.insights.models import BusinessPolicy

    trigger = policy.trigger
    condition = policy.condition or {}

    if trigger == BusinessPolicy.Trigger.CONVERSATION_UNANSWERED:
        return _trigger_conversation_unanswered(account, condition)
    if trigger == BusinessPolicy.Trigger.LEAD_UNANSWERED:
        return _trigger_lead_unanswered(account, condition)
    if trigger == BusinessPolicy.Trigger.CUSTOMER_INACTIVE:
        return _trigger_customer_inactive(account, condition)
    if trigger == BusinessPolicy.Trigger.REPURCHASE_DUE:
        return _trigger_repurchase_due(account)
    return []


def _trigger_conversation_unanswered(account, condition) -> list:
    from apps.conversations.state import needs_attention

    hours = int(condition.get("hours", 2))
    cutoff = timezone.now() - timedelta(hours=hours)
    qs = (
        needs_attention(account, timezone.now())
        .filter(messages__created_at__lt=cutoff)
        .distinct()
    )
    return list(qs)


def _trigger_lead_unanswered(account, condition) -> list:
    from apps.conversations.state import needs_attention

    hours = int(condition.get("hours", 24))
    cutoff = timezone.now() - timedelta(hours=hours)
    qs = (
        needs_attention(account, timezone.now())
        .filter(messages__created_at__lt=cutoff)
        .distinct()
    )
    return list(qs)


def _trigger_customer_inactive(account, condition) -> list:
    from apps.contacts.models import Contact

    days = int(condition.get("days", 90))
    cutoff = timezone.now() - timedelta(days=days)
    qs = Contact.objects.filter(
        account=account,
        lifecycle_stage__in=["customer", "repeat_customer"],
        last_engaged_at__lt=cutoff,
    )
    return list(qs)


def _trigger_repurchase_due(account) -> list:
    from apps.insights.rules import overdue_repurchase_contacts

    # overdue_repurchase_contacts returns (rows, interval_days); we only need the rows.
    overdue_rows, _ = overdue_repurchase_contacts(account)
    # Each row is an order-dict; the executor needs contact_ids for target building.
    # Return the rows directly — _build_target handles the dict format.
    return overdue_rows


# ---------------------------------------------------------------------------
# Idempotency guards  (action-aware, not target-set-based)
# ---------------------------------------------------------------------------


def _already_executed(policy, target_payload: dict) -> bool:
    """True if this policy should be suppressed to prevent duplicate dispatch."""
    action_type = policy.action.get("type", "")
    if action_type == "campaign":
        return _already_executed_campaign(policy)
    # create_followup uses get_or_create — deduplication happens at the DB level.
    # notify_owner is always deferred — never suppress.
    return False


def _already_executed_campaign(policy) -> bool:
    """True if this policy already dispatched its campaign within the execution window.

    Window = condition["days"] * 24h (or condition["hours"] * 2 for hourly triggers).
    For campaign actions the window is derived from the condition so a 90-day
    inactivity policy won't re-fire its campaign for 90 days.
    """
    from apps.insights.models import PolicyExecution

    condition = policy.condition or {}
    # Use the longest plausible window: days-based conditions use days, hours-based use hours.
    if "days" in condition:
        window = timedelta(days=int(condition["days"]))
    elif "hours" in condition:
        window = timedelta(hours=int(condition["hours"]) * 2)
    else:
        window = timedelta(hours=48)

    campaign_id = policy.action.get("campaign_id")
    if not campaign_id:
        return False

    return PolicyExecution.objects.filter(
        policy=policy,
        status=PolicyExecution.Status.COMPLETED,
        started_at__gte=timezone.now() - window,
        result__action="campaign",
        result__campaign_id=campaign_id,
        result__dispatch="queued",
    ).exists()


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------


def _execute_action(policy, targets: list, account) -> dict:
    action_type = policy.action.get("type", "")
    if action_type == "campaign":
        return _execute_campaign(policy, targets, account)
    if action_type == "create_followup":
        return _execute_followup(policy, targets, account)
    if action_type == "notify_owner":
        return _execute_notify_owner(policy, account)
    # Should be unreachable after the activation guard, but stay defensive.
    raise PolicyActionError(f"Unknown action type: {action_type!r}")


def _execute_campaign(policy, targets, account) -> dict:
    from apps.whatsapp.campaigns import send_campaign

    campaign_id = policy.action.get("campaign_id")
    if not campaign_id:
        raise PolicyActionError(
            f"Policy {policy.pk} action.campaign_id is missing — cannot dispatch campaign."
        )
    send_campaign.delay(campaign_id)
    return {"action": "campaign", "campaign_id": campaign_id, "dispatch": "queued"}


def _execute_followup(policy, targets, account) -> dict:
    from apps.conversations.models import FollowUp

    due_hours = int(policy.action.get("due_hours", 4))
    due_at = timezone.now() + timedelta(hours=due_hours)
    note = policy.action.get("note") or policy.name

    created_count = 0
    existing_count = 0

    for target in targets:
        # targets for follow-up triggers are Conversation instances.
        if not hasattr(target, "account_id"):
            continue
        defaults: dict = {
            "due_at": due_at,
            "note": note,
            "source": "policy",
        }
        contact = getattr(target, "contact", None)
        if contact is not None:
            defaults["contact"] = contact
        _, created = FollowUp.objects.get_or_create(
            account=account,
            conversation=target,
            done_at__isnull=True,
            defaults=defaults,
        )
        if created:
            created_count += 1
        else:
            existing_count += 1

    return {
        "action": "create_followup",
        "followup_count": created_count + existing_count,
        "created_count": created_count,
        "existing_count": existing_count,
    }


def _execute_notify_owner(policy, account) -> dict:
    # notify_owner requires a persistent notification model not yet built.
    # Deliberately deferred: log the intent, return COMPLETED so the execution
    # ledger records the decision rather than a false failure.
    logger.warning(
        "executor: notify_owner action deferred — no notification model yet "
        "(policy=%s account=%s)",
        policy.pk,
        account.pk,
    )
    return {"action": "notify_owner", "dispatch": "deferred"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_target(targets: list) -> dict:
    """Build the typed target payload for PolicyExecution.target."""
    if not targets:
        return {"type": "unknown", "count": 0, "ids": []}

    first = targets[0]
    # Determine target type from the first element.
    if isinstance(first, dict):
        # order-row dicts from _trigger_repurchase_due
        target_type = "contact"
        ids = [t.get("contact_id") for t in targets[:50] if t.get("contact_id")]
    elif hasattr(first, "contact_id"):
        # Conversation instances
        target_type = "conversation"
        ids = [t.pk for t in targets[:50]]
    else:
        # Contact instances
        target_type = "contact"
        ids = [t.pk for t in targets[:50]]

    return {"type": target_type, "count": len(targets), "ids": ids}


def _mark_pe(pe, status, *, result: dict | None = None, error: str = "") -> None:
    """Update a PolicyExecution row to its final state."""
    pe.status = status
    pe.completed_at = timezone.now()
    if result is not None:
        pe.result = result
    if error:
        pe.error = error
    update_fields = ["status", "completed_at"]
    if result is not None:
        update_fields.append("result")
    if error:
        update_fields.append("error")
    pe.save(update_fields=update_fields)


def _attach_to_recommendation(policy, pe) -> None:
    """Link the PolicyExecution to the accepted RecommendationLog for the source insight.

    This wires the explicit audit chain:
        Insight → RecommendationLog (accepted) → BusinessPolicy → PolicyExecution

    Does NOT update outcome_* fields — execution is not yet a business outcome.
    """
    from apps.insights.models import RecommendationLog

    insight = policy.created_from
    if insight is None:
        return

    rec_log = RecommendationLog.objects.filter(
        insight=insight,
        account=policy.account,
        accepted=True,
        status=RecommendationLog.Status.ACCEPTED,
    ).first()
    if rec_log is None:
        return

    rec_log.policy_execution = pe
    rec_log.save(update_fields=["policy_execution"])
