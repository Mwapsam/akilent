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
        proposal.model, proposal.provider, proposal.latency_ms = out["model"][:80], out["provider"][:40], out["latency_ms"]
        proposal.status, proposal.ready_at = AIProposal.Status.READY, timezone.now()
        proposal.save()
        # One live proposal per conversation: older ready ones are overtaken.
        AIProposal.objects.filter(
            conversation=conversation, status=AIProposal.Status.READY,
        ).exclude(pk=proposal.pk).update(status=AIProposal.Status.EXPIRED, expired_at=timezone.now())
        return "ready"
    except Retry:
        raise
    except Exception:  # noqa: BLE001 - a proposal must never crash the worker loop
        logger.exception("draft_proposal failed for proposal=%s", proposal_id)
        return _finish(proposal, AIProposal.Status.ERROR, error="Something went wrong drafting this.")
    finally:
        if cache.get(_lock_key(conversation.pk)) == proposal.pk:
            cache.delete(_lock_key(conversation.pk))


def _finish(proposal, status: str, *, error: str = "") -> str:
    proposal.status = status
    proposal.error = error[:300]
    if status == "expired":
        proposal.expired_at = timezone.now()
    proposal.save(update_fields=["status", "error", "expired_at"])
    return status
