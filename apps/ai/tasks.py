"""The AI worker: turns a queued proposal into a ready (or failed) one. Runs on the ``ai`` queue.

Never in a web request. Serialised per conversation with a cache lock, and it coalesces: if the
customer has written again since this proposal was queued, this one steps aside and the newer
message's proposal answers the whole burst. A timeout or network error is retried with backoff;
any other failure is recorded on the proposal and never raised further.
"""
from __future__ import annotations

import logging

from celery import shared_task
from celery.exceptions import Retry
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2
_LOCK_SECONDS = 120


def _lock_key(conversation_id) -> str:
    return f"ai-lock:conversation:{conversation_id}"


def _daily_key(account_id) -> str:
    return f"ai-calls:{account_id}:{timezone.now().date().isoformat()}"


def _over_daily_limit(account_id) -> bool:
    from django.conf import settings

    limit = int(getattr(settings, "AI_DAILY_CALL_LIMIT", 500) or 0)
    if limit <= 0:
        return False
    key = _daily_key(account_id)
    cache.add(key, 0, 26 * 3600)
    try:
        return cache.incr(key) > limit
    except ValueError:
        return False


@shared_task(bind=True, max_retries=_MAX_RETRIES, queue="ai")
def draft_proposal(self, proposal_id: int) -> str:
    from apps.ai import agent
    from apps.ai.models import AIProposal, AISettings
    from apps.ai.proposals import ProposalError
    from apps.ai.providers import AIProviderError
    from apps.conversations.api import newest_message_ids

    proposal = AIProposal.objects.select_related("conversation", "account").filter(pk=proposal_id).first()
    if proposal is None or proposal.status != AIProposal.Status.PENDING:
        return "skipped"
    conversation = proposal.conversation
    if conversation is None or conversation.status != "open":
        return _finish(proposal, AIProposal.Status.EXPIRED)

    # An automatic proposal answers one message. If the customer wrote again, or someone (a
    # person or an automation) has already replied, it's no longer needed.
    if proposal.trigger_message_id:
        newest_inbound, newest_outbound = newest_message_ids(conversation)
        if newest_inbound != proposal.trigger_message_id or (
                newest_outbound and newest_outbound > proposal.trigger_message_id):
            return _finish(proposal, AIProposal.Status.EXPIRED)

    if not cache.add(_lock_key(conversation.pk), proposal.pk, _LOCK_SECONDS):
        raise self.retry(countdown=5)
    try:
        if _over_daily_limit(proposal.account_id):
            return _finish(proposal, AIProposal.Status.ERROR, error="The daily AI limit has been reached.")
        ai_settings = AISettings.objects.filter(account_id=proposal.account_id).first()
        try:
            out = agent.run(conversation, business_notes=ai_settings.business_notes if ai_settings else "")
        except AIProviderError as exc:
            if self.request.retries < self.max_retries:
                cache.delete(_lock_key(conversation.pk))
                raise self.retry(countdown=10 * (2 ** self.request.retries), exc=exc)
            return _finish(proposal, AIProposal.Status.ERROR, error=str(exc))
        except ProposalError as exc:
            return _finish(proposal, AIProposal.Status.ERROR, error=str(exc))

        p = out["proposal"]
        proposal.version, proposal.action = p["version"], p["action"]
        proposal.confidence, proposal.reason, proposal.payload = p["confidence"], p["reason"], p["payload"]
        proposal.extras = [dict(e, applied_at=None) for e in p.get("extras", [])]
        proposal.tools_used = out.get("tools_used", [])
        proposal.intent, proposal.route = p.get("intent", "")[:20], out.get("route", "")[:10]
        proposal.model, proposal.provider, proposal.latency_ms = out["model"][:80], out["provider"][:40], out["latency_ms"]
        proposal.status, proposal.ready_at = AIProposal.Status.READY, timezone.now()
        proposal.save()
        # One live proposal per conversation: older ready ones are overtaken.
        AIProposal.objects.filter(
            conversation=conversation, status=AIProposal.Status.READY,
        ).exclude(pk=proposal.pk).update(status=AIProposal.Status.EXPIRED, expired_at=timezone.now())
        _maybe_refresh_memory(conversation)
        return _maybe_send_on_its_own(proposal, p, out, ai_settings)
    except Retry:
        raise
    except Exception:  # noqa: BLE001 - a proposal must never crash the worker loop
        logger.exception("draft_proposal failed for proposal=%s", proposal_id)
        return _finish(proposal, AIProposal.Status.ERROR, error="Something went wrong drafting this.")
    finally:
        if cache.get(_lock_key(conversation.pk)) == proposal.pk:
            cache.delete(_lock_key(conversation.pk))


def _maybe_send_on_its_own(proposal, contract: dict, out: dict, ai_settings) -> str:
    """For a business that chose automatic replies: send if every check passes, else leave the
    suggestion for a person (the fallback is exactly suggest-only behaviour). Returns the task result.
    """
    from apps.ai import autonomy
    from apps.ai.models import AIProposal
    from apps.conversations.api import newest_message_ids
    from apps.core.actions import ActionError, run_action

    if not autonomy.is_auto_mode(ai_settings):
        return "ready"
    conversation = proposal.conversation
    decision = autonomy.evaluate(
        ai_settings=ai_settings, proposal=contract, conversation=conversation,
        automatic=proposal.trigger_message_id is not None and proposal.requested_by_id is None,
        window_open=out.get("window_open", False), facts=out.get("facts") or {},
        extra_text=out.get("sources", ""), trigger_message_id=proposal.trigger_message_id,
    )
    record = decision.as_dict()
    if decision.send:
        # Drafting took a while: re-check nobody replied and the customer didn't write again.
        newest_inbound, newest_outbound = newest_message_ids(conversation)
        if newest_inbound != proposal.trigger_message_id or (
                newest_outbound and newest_outbound > proposal.trigger_message_id):
            record.update(send=False, error="The conversation moved on while AI was drafting.")
        else:
            try:
                run_action("reply", {"account": proposal.account}, conversation=conversation,
                           body=contract["payload"]["text"], idempotency_key=f"ai-auto:{proposal.pk}")
            except ActionError as exc:
                record.update(send=False, error=str(exc)[:300])
            except Exception:  # noqa: BLE001 - a failed send falls back to a suggestion
                logger.exception("automatic AI reply failed for proposal=%s", proposal.pk)
                record.update(send=False, error="Sending failed.")
    proposal.auto_decision = record
    fields = ["auto_decision"]
    if record["send"]:
        now = timezone.now()
        proposal.status, proposal.used_at, proposal.auto_sent_at = AIProposal.Status.USED, now, now
        proposal.edited_before_send = False
        fields += ["status", "used_at", "auto_sent_at", "edited_before_send"]
    proposal.save(update_fields=fields)
    return "sent" if record["send"] else "ready"


def _maybe_refresh_memory(conversation) -> None:
    """Queue a memory refresh once enough older messages have built up. Never fails a draft."""
    from apps.ai import memory

    try:
        if memory.needs_refresh(conversation):
            refresh_memory.apply_async((conversation.pk,), countdown=5, queue="ai")
    except Exception:  # noqa: BLE001
        logger.exception("could not queue AI memory refresh for conversation=%s", conversation.pk)


def _memory_lock_key(conversation_id) -> str:
    return f"ai-memory-lock:conversation:{conversation_id}"


@shared_task(bind=True, max_retries=_MAX_RETRIES, queue="ai")
def refresh_memory(self, conversation_id: int) -> str:
    """Fold a long conversation's older messages into its AI memory, one batch per run."""
    from apps.ai import api as ai_api
    from apps.ai import memory
    from apps.ai.proposals import ProposalError
    from apps.ai.providers import AIProviderError, get_ai_provider
    from apps.conversations.api import get_conversation_by_id

    conversation = get_conversation_by_id(conversation_id)
    if conversation is None or not ai_api.is_available(conversation.account):
        return "skipped"
    if not cache.add(_memory_lock_key(conversation_id), 1, _LOCK_SECONDS):
        return "busy"
    try:
        if _over_daily_limit(conversation.account_id):
            return "limit"
        try:
            updated = memory.refresh(conversation, get_ai_provider(conversation.account, tier="fast"))
        except AIProviderError as exc:
            if self.request.retries < self.max_retries:
                cache.delete(_memory_lock_key(conversation_id))
                raise self.retry(countdown=30 * (2 ** self.request.retries), exc=exc)
            return "error"
        except ProposalError:
            return "error"
        if updated and memory.needs_refresh(conversation):  # a long backlog: keep folding
            refresh_memory.apply_async((conversation_id,), countdown=2, queue="ai")
        return "updated" if updated else "nothing"
    except Retry:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("refresh_memory failed for conversation=%s", conversation_id)
        return "error"
    finally:
        cache.delete(_memory_lock_key(conversation_id))


@shared_task(bind=True, max_retries=_MAX_RETRIES, queue="ai")
def build_draft(self, draft_id: int) -> str:
    """Fill one setup draft (automation, template, template edit). Never creates the real thing."""
    from apps.ai import drafting
    from apps.ai.models import AIDraft
    from apps.ai.providers import AIProviderError, get_ai_provider

    draft = AIDraft.objects.select_related("account").filter(pk=draft_id).first()
    if draft is None or draft.status != AIDraft.Status.PENDING:
        return "skipped"

    def fail(message: str) -> str:
        draft.status, draft.error = AIDraft.Status.ERROR, message[:300]
        draft.save(update_fields=["status", "error"])
        return "error"

    if _over_daily_limit(draft.account_id):
        return fail("The daily AI limit has been reached. Try again tomorrow.")
    try:
        drafting.run(draft, get_ai_provider(draft.account))
    except AIProviderError as exc:
        if self.request.retries < self.max_retries:
            raise self.retry(countdown=5 * (2 ** self.request.retries), exc=exc)
        return fail(str(exc))
    except drafting.DraftError as exc:
        return fail(str(exc))
    except Retry:
        raise
    except Exception:  # noqa: BLE001 - a draft must never crash the worker loop
        logger.exception("build_draft failed for draft=%s", draft_id)
        return fail("Something went wrong drafting this. Try again.")
    draft.status = AIDraft.Status.READY
    draft.save(update_fields=["status", "result", "warnings", "model"])
    return "ready"


def _finish(proposal, status: str, *, error: str = "") -> str:
    proposal.status = status
    proposal.error = error[:300]
    if status == "expired":
        proposal.expired_at = timezone.now()
    proposal.save(update_fields=["status", "error", "expired_at"])
    return status
