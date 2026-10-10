"""Reply-correlation service for workflow interactions (A0).

``claim_reply`` is the single entry point for routing an inbound message to a
waiting workflow interaction. It guarantees that:

- An answer is never consumed without a run state transition.
- A duplicate provider message never resumes a run twice.
- Two concurrent typed messages resume at most one run each, in a defined order.
- An expiry/answer race yields exactly one transition.
- A continuation is durable: the run state is committed before ``advance_run``
  is called, and the ``run_due`` sweeper picks it up if the fast path crashes.

Lock order (always, everywhere in this module):
  Conversation row → open interactions (ascending created_at, pk) → WorkflowRun

Result codes
------------
``claimed``          – the interaction was matched and the run advanced.
``already_claimed``  – this provider message was already processed (duplicate).
                       Processing stops; the message does NOT go to the pipeline.
``unmatched``        – no open interaction matched; the message goes to the pipeline.
``stale_run``        – an interaction was found but its run was no longer WAITING on
                       that step. The orphaned interaction is cancelled. The message
                       goes to the pipeline.
``wrong_conversation`` – a token for an interaction on a different conversation.
                         The message goes to the pipeline.
"""

from __future__ import annotations

import logging
from typing import Literal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.automation.models import (
    InteractionAnswer,
    WorkflowInteraction,
    WorkflowRun,
    WorkflowStepRun,
)

logger = logging.getLogger(__name__)

ClaimResult = Literal["claimed", "already_claimed", "unmatched", "stale_run", "wrong_conversation"]

# A stalled continuation: ACTIVE run with next_due_at in the past by more than this.
_STALL_SECONDS = 60


def claim_reply(
    conversation,
    message_key: str,
    reply_id: str,
    body: str,
) -> ClaimResult:
    """Route an inbound message to the correct waiting interaction.

    Args:
        conversation: The spine ``Conversation`` the message arrived in.
        message_key:  ``"{channel}:{provider_account_id}:{provider_message_id}"``.
        reply_id:     For interactive replies this is ``"{token}:{option_id}"``;
                      for typed messages it is empty or the raw text (unused here
                      for routing — ``body`` is used instead).
        body:         The plain-text body of the message.

    Returns:
        A ``ClaimResult`` string (see module docstring).
    """
    # ── Pre-check: duplicate message key (outside the main transaction) ─────────
    # If we already claimed this message_key, return early without taking any locks.
    # This is a best-effort fast path; the savepoint inside the transaction is the
    # real idempotency backstop.
    if InteractionAnswer.objects.filter(message_key=message_key).exists():
        logger.info("claim_reply already_claimed (pre-check): message_key=%s", message_key)
        return "already_claimed"

    with transaction.atomic():
        # ── Step 1: lock the conversation row (anchor, even when 0 interactions) ──
        from apps.conversations.models import Conversation

        conversation = (
            Conversation.objects.select_for_update()
            .filter(pk=conversation.pk)
            .get()
        )

        # Re-check inside the transaction (under the conversation lock).
        if InteractionAnswer.objects.filter(message_key=message_key).exists():
            logger.info("claim_reply already_claimed (post-lock): message_key=%s", message_key)
            return "already_claimed"

        # ── Step 2: lock all open interactions on this conversation ──────────────
        # Ascending (created_at, pk) is the canonical lock order.
        open_interactions = list(
            WorkflowInteraction.objects.select_for_update()
            .filter(conversation=conversation, status=WorkflowInteraction.Status.OPEN)
            .order_by("created_at", "pk")
            .select_related("run__workflow")
        )

        if not open_interactions:
            # If the reply contains a token for an interaction on a different
            # conversation, detect it here before falling through to unmatched.
            parsed = _parse_token_option(reply_id)
            if parsed is not None:
                token, _ = parsed
                try:
                    other = WorkflowInteraction.objects.get(token=token)
                    if other.conversation_id != conversation.pk:
                        logger.info(
                            "claim_reply wrong_conversation (no open): token=%s interaction=%s",
                            token, other.pk,
                        )
                        return "wrong_conversation"
                except WorkflowInteraction.DoesNotExist:
                    pass
            return "unmatched"

        # ── Step 3: choose the target interaction ─────────────────────────────────
        target_interaction, option_id = _pick_target(
            open_interactions, reply_id, body, conversation
        )

        if target_interaction is None:
            return "unmatched"

        # Cross-conversation token rejection is handled inside _pick_target; if we
        # reach here with a token, the conversation matched.
        if isinstance(target_interaction, str):
            # _pick_target returns "wrong_conversation" as a sentinel string.
            return target_interaction  # type: ignore[return-value]

        # ── Step 4: lock the run and re-check state ───────────────────────────────
        run = (
            WorkflowRun.objects.select_for_update()
            .filter(pk=target_interaction.run_id)
            .get()
        )

        if (
            run.status != WorkflowRun.Status.WAITING
            or run.current_step != target_interaction.step_id
        ):
            # Orphan: the interaction is open but its run has already moved on.
            # Cancel the interaction so it never blocks later messages.
            target_interaction.status = WorkflowInteraction.Status.CANCELLED
            target_interaction.closed_at = timezone.now()
            target_interaction.save(update_fields=["status", "closed_at"])
            logger.info(
                "claim_reply stale_run: interaction=%s run=%s step=%s run_status=%s run_step=%s",
                target_interaction.pk,
                run.pk,
                target_interaction.step_id,
                run.status,
                run.current_step,
            )
            return "stale_run"

        # ── Step 5: durable claim via InteractionAnswer (savepoint) ──────────────
        try:
            with transaction.atomic():  # savepoint
                InteractionAnswer.objects.create(
                    message_key=message_key,
                    interaction=target_interaction,
                )
        except IntegrityError:
            # Duplicate provider message — already processed.
            logger.info(
                "claim_reply already_claimed: message_key=%s",
                message_key,
            )
            return "already_claimed"

        # ── Step 6: transition atomically (all or nothing) ───────────────────────
        _commit_claim(run, target_interaction, option_id, body)

    # ── After commit: fast-path continuation ─────────────────────────────────────
    # advance_run is called outside the outer transaction so it gets its own
    # savepoints. Any crash here is safe — run_due will pick up the ACTIVE run.
    try:
        _advance_after_commit(run)
    except Exception:
        logger.exception("claim_reply: _advance_after_commit raised run=%s", run.pk)

    logger.info(
        "claim_reply claimed: message_key=%s interaction=%s run=%s",
        message_key,
        target_interaction.pk,
        run.pk,
    )
    return "claimed"


# ── Internal helpers ──────────────────────────────────────────────────────────


def _parse_token_option(reply_id: str) -> tuple[str, str] | None:
    """If reply_id is ``"{token}:{option_id}"`` return ``(token, option_id)``, else None."""
    if not reply_id:
        return None
    parts = reply_id.split(":", 1)
    if len(parts) != 2 or len(parts[0]) != 22:  # token_urlsafe(16) = 22 chars
        return None
    return parts[0], parts[1]


def _pick_target(
    open_interactions: list,
    reply_id: str,
    body: str,
    conversation,
) -> tuple[WorkflowInteraction | str | None, str]:
    """Return (interaction, option_id) or ("wrong_conversation", "") or (None, "").

    The typed-reply selection policy ("most recently eligible") is applied in
    Python *after* all locks are held, satisfying the lock-then-select contract.
    """
    from apps.automation import keywords

    parsed = _parse_token_option(reply_id)

    if parsed is not None:
        token, option_id = parsed
        # Interactive reply: look for the matching token in the locked set.
        for interaction in open_interactions:
            if interaction.token == token:
                return interaction, option_id
        # Token found but not in the locked set — could be a different conversation.
        # Check if this token exists at all (it may belong to another conversation).
        try:
            other = WorkflowInteraction.objects.get(token=token)
            if other.conversation_id != conversation.pk:
                logger.info(
                    "claim_reply wrong_conversation: token=%s interaction=%s",
                    token,
                    other.pk,
                )
                return "wrong_conversation", ""
            # Token on this conversation but already answered/expired/cancelled.
            # Don't fall through to typed-text matching — the tap is stale.
            return None, ""
        except WorkflowInteraction.DoesNotExist:
            pass
        # Token genuinely unknown — treat as typed text below.

    # Typed reply: find interactions that accept free text or have a matching route.
    # Apply newest-first selection after locks are held (but iterate in created_at,pk
    # order to avoid changing the lock acquisition order).
    candidates = []
    normalized_body = keywords.normalize(body) if body else ""

    for interaction in open_interactions:
        expected = interaction.expected or {}
        option_ids = expected.get("option_ids", [])
        free_text = expected.get("free_text", False)

        if free_text:
            candidates.append((interaction, ""))
            continue

        # Check if typed text matches one of the known option ids or titles.
        option_titles = expected.get("option_titles", [])
        for oid, title in zip(option_ids, option_titles + [None] * len(option_ids)):
            if normalized_body and (
                normalized_body == keywords.normalize(str(oid))
                or (title and normalized_body == keywords.normalize(str(title)))
            ):
                candidates.append((interaction, oid))
                break

    if not candidates:
        return None, ""

    if len(candidates) > 1:
        # Log ambiguity but proceed with the newest (largest created_at, pk).
        logger.info(
            "claim_reply ambiguous typed reply: %d candidates, interaction_ids=%s run_ids=%s",
            len(candidates),
            [i.pk for i, _ in candidates],
            [i.run_id for i, _ in candidates],
        )

    # "Newest eligible" = last in the ascending-lock order.
    return candidates[-1]


def _commit_claim(
    run: WorkflowRun,
    interaction: WorkflowInteraction,
    option_id: str,
    body: str,
) -> None:
    """Commit the interaction answer and run state in one transaction.

    Called inside the outer ``transaction.atomic()`` from ``claim_reply``.
    The durable continuation fields are set here so ``run_due`` can recover
    if ``_advance_after_commit`` never runs.
    """
    steps = _steps_by_id(run.workflow)
    step = steps.get(interaction.step_id)

    # ask_question steps have their own answer-processing path.
    if (step or {}).get("type") == "ask_question":
        _commit_ask_question_answer(run, interaction, body, step)
        return

    # Determine the next step via the existing route helper.
    from apps.automation.workflow_engine import _route_for_reply

    # Use option_id (not the full token:option_id) for route matching.
    # _route_for_reply checks reply_id against step.routes keys.
    message = {
        "reply_id": option_id or "",
        "reply_title": option_id,
        "body": body,
    }
    target = _route_for_reply(step or {}, message) if step else None
    if not target:
        # For IG send_buttons, routes are often absent; fall back to `next`.
        # Never use `on_timeout` here — that path is for expiry, not live replies.
        target = (step or {}).get("next") or ""

    # If the target step is a wait_for_reply and the option_id matches one of its routes,
    # thread through inline rather than parking the customer for a second reply.
    if target and option_id:
        next_step = steps.get(target)
        if next_step and next_step.get("type") == "wait_for_reply":
            threaded = _route_for_reply(next_step, {"reply_id": option_id, "body": ""})
            if threaded:
                target = threaded

    # Record the step result with the real button title (not the slugified option id).
    _option_ids = (interaction.expected or {}).get("option_ids", [])
    _option_titles = (interaction.expected or {}).get("option_titles", [])
    _real_title = option_id
    for _oid, _t in zip(_option_ids, _option_titles):
        if _oid == option_id and _t:
            _real_title = _t
            break
    reply_ctx = {
        "id": option_id,
        "title": _real_title,
        "text": body,
    }
    # The send_buttons row was already created at enrollment; merge the reply result in.
    step_run, created = WorkflowStepRun.objects.get_or_create(
        run=run,
        step_id=interaction.step_id,
        defaults={
            "step_type": (step or {}).get("type", ""),
            "status": "ok",
            "result": {"reply": reply_ctx, "went_to": target},
        },
    )
    if not created:
        step_run.result = {**step_run.result, "reply": reply_ctx, "went_to": target}
        step_run.save(update_fields=["result"])

    # Mark the interaction answered.
    interaction.status = WorkflowInteraction.Status.ANSWERED
    interaction.closed_at = timezone.now()
    interaction.save(update_fields=["status", "closed_at"])

    # Set durable continuation: ACTIVE + next step + next_due_at=now so run_due
    # can sweep it up if advance_run never runs.
    run.context = {**(run.context or {}), "reply": reply_ctx}
    run.status = WorkflowRun.Status.ACTIVE
    run.current_step = target
    run.next_due_at = timezone.now()  # sweeper trigger
    run.save(update_fields=["context", "status", "current_step", "next_due_at"])


def _commit_ask_question_answer(
    run: WorkflowRun,
    interaction: WorkflowInteraction,
    body: str,
    step: dict,
) -> None:
    """Handle a free-text answer for an ask_question step.

    All of the following are committed in the outer transaction (the same
    ``transaction.atomic()`` in ``claim_reply``):
    - Attribute write + attribute.changed event (valid path)
    - Interaction state transition (answered or invalid_answer_count++)
    - Re-prompt OutboundMessage row (invalid + attempts remaining)
    - WorkflowStepRun (terminal answer only)
    - Run durable-continuation fields

    The ``InteractionAnswer`` row was already inserted (savepoint) before this
    function is called.
    """
    from apps.contacts.attributes import _coerce, set_attributes
    from apps.contacts.models import CustomAttributeDef

    step_id = interaction.step_id
    attribute_key = step.get("attribute", "")
    target_entity = step.get("target", "contact")
    max_attempts = int(step.get("max_attempts", 1))
    timeout_seconds = int(step.get("timeout_seconds", 86400))
    on_invalid = step.get("on_invalid") or step.get("next") or ""
    on_next = step.get("next") or ""

    # Resolve the target object (contact / lead / deal).
    target_obj = _resolve_ask_target(run, target_entity)

    # Attempt coercion (no lock here; set_attributes takes the authoritative lock below).
    coerced = None
    coerce_error = None
    if target_obj is not None and attribute_key:
        try:
            defn = CustomAttributeDef.objects.get(
                account=run.workflow.account,
                entity=target_entity,
                key=attribute_key,
                archived_at__isnull=True,
            )
            coerced = _coerce(body, defn)
        except CustomAttributeDef.DoesNotExist:
            coerce_error = f"attribute def {attribute_key!r} not found for entity {target_entity!r}"
        except ValueError as exc:
            coerce_error = str(exc)
    else:
        coerce_error = "no target object or attribute key"

    invalid_count = interaction.invalid_answer_count
    is_valid = coerce_error is None

    if is_valid:
        # ── Valid answer ──────────────────────────────────────────────────────
        set_attributes(target_obj, {attribute_key: coerced}, source="workflow")

        interaction.status = WorkflowInteraction.Status.ANSWERED
        interaction.closed_at = timezone.now()
        interaction.save(update_fields=["status", "closed_at"])

        WorkflowStepRun.objects.get_or_create(
            run=run,
            step_id=step_id,
            defaults={
                "step_type": "ask_question",
                "status": "ok",
                "result": {
                    "attribute": attribute_key,
                    "target": target_entity,
                    "went_to": on_next,
                },
            },
        )

        run.status = WorkflowRun.Status.ACTIVE
        run.current_step = on_next
        run.next_due_at = timezone.now()
        run.save(update_fields=["status", "current_step", "next_due_at"])

    elif invalid_count < max_attempts - 1:
        # ── Invalid, re-prompt ────────────────────────────────────────────────
        # invalid_answer_count counts answers seen so far (0-indexed).
        # With max_attempts=2: first invalid → re-prompt; second → on_invalid.
        new_count = invalid_count + 1
        interaction.invalid_answer_count = new_count
        # Reset deadline to now + timeout (only re-prompts reset the deadline).
        new_deadline = timezone.now() + __import__("datetime").timedelta(seconds=timeout_seconds)
        interaction.deadline_at = new_deadline
        interaction.save(update_fields=["invalid_answer_count", "deadline_at"])

        # Enqueue the re-prompt message (DB-backed; delivered once by the worker).
        # Use the interaction's own conversation — not a freshly resolved one —
        # so the re-prompt arrives in the same thread as the original question.
        reprompt_text = step.get("reprompt", step.get("question", ""))
        if reprompt_text and interaction.conversation_id:
            from apps.automation.workflow_engine import _send_ask_question_text
            from apps.conversations.models import Conversation

            try:
                conv = Conversation.objects.get(pk=interaction.conversation_id)
                _send_ask_question_text(run, conv, reprompt_text, step_id, attempt=new_count)
            except Conversation.DoesNotExist:
                logger.warning(
                    "claim_reply ask_question re-prompt: conversation %s not found run=%s",
                    interaction.conversation_id, run.pk,
                )

        # Keep the run WAITING; sync next_due_at to the new deadline invariant.
        run.next_due_at = new_deadline
        run.save(update_fields=["next_due_at"])

        logger.info(
            "claim_reply ask_question re-prompt: run=%s step=%s attempt=%s reason=%s",
            run.pk, step_id, new_count, coerce_error,
        )

    else:
        # ── Invalid, exhausted ────────────────────────────────────────────────
        interaction.status = WorkflowInteraction.Status.ANSWERED
        interaction.closed_at = timezone.now()
        interaction.save(update_fields=["status", "closed_at"])

        WorkflowStepRun.objects.get_or_create(
            run=run,
            step_id=step_id,
            defaults={
                "step_type": "ask_question",
                "status": "ok",
                "result": {
                    "attribute": attribute_key,
                    "target": target_entity,
                    "invalid": True,
                    "went_to": on_invalid,
                },
            },
        )

        run.status = WorkflowRun.Status.ACTIVE
        run.current_step = on_invalid
        run.next_due_at = timezone.now()
        run.save(update_fields=["status", "current_step", "next_due_at"])

        logger.info(
            "claim_reply ask_question exhausted: run=%s step=%s reason=%s → %s",
            run.pk, step_id, coerce_error, on_invalid,
        )


def _resolve_ask_target(run: WorkflowRun, target_entity: str):
    """Return the target object (contact/lead/deal) for attribute writes."""
    if target_entity == "contact":
        return run.contact
    if target_entity == "lead":
        try:
            from apps.crm.models import Lead

            return Lead.objects.filter(
                contact=run.contact, account=run.workflow.account
            ).order_by("-created_at").first()
        except Exception:
            return None
    if target_entity == "deal":
        try:
            from apps.crm.models import Deal

            return Deal.objects.filter(
                contact=run.contact, account=run.workflow.account
            ).order_by("-created_at").first()
        except Exception:
            return None
    return None



def _advance_after_commit(run: WorkflowRun) -> None:
    """Fast-path continuation after the claim commits. Safe to crash."""
    try:
        from apps.automation.workflow_engine import advance_run

        # Re-fetch to get the committed state (the in-memory run has next_due_at=now).
        run.refresh_from_db()
        with transaction.atomic():
            # advance_run takes a select_for_update lock inside (see workflow_engine),
            # so if run_due got there first, the loser returns unchanged.
            locked_run = WorkflowRun.objects.select_for_update().get(pk=run.pk)
            if locked_run.status == WorkflowRun.Status.ACTIVE:
                locked_run.next_due_at = None
                locked_run.save(update_fields=["next_due_at"])
                advance_run(locked_run)
    except Exception:
        logger.exception("claim_reply: advance_after_commit failed run=%s", run.pk)


def _steps_by_id(workflow) -> dict:
    from apps.automation.workflow_engine import _steps_by_id as _engine_steps_by_id

    return _engine_steps_by_id(workflow)


def cancel_open_interactions_for_run(run: WorkflowRun) -> int:
    """Cancel all open interactions for a run (e.g. when the run is cancelled/failed).

    Must be called while the run row is already locked. Uses skip_locked so that
    interactions already held by a concurrent claim_reply (lock order: interaction
    then run) are skipped rather than causing a deadlock — those are about to be
    answered, so skipping them is correct.
    """
    from django.db import NotSupportedError

    now = timezone.now()
    try:
        locked_ids = list(
            WorkflowInteraction.objects.select_for_update(skip_locked=True)
            .filter(run=run, status=WorkflowInteraction.Status.OPEN)
            .values_list("pk", flat=True)
        )
    except NotSupportedError:
        # SQLite doesn't support skip_locked; plain update is safe in single-process tests.
        return WorkflowInteraction.objects.filter(
            run=run, status=WorkflowInteraction.Status.OPEN,
        ).update(status=WorkflowInteraction.Status.CANCELLED, closed_at=now)
    if not locked_ids:
        return 0
    return WorkflowInteraction.objects.filter(
        pk__in=locked_ids,
    ).update(status=WorkflowInteraction.Status.CANCELLED, closed_at=now)
