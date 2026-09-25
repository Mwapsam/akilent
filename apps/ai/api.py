"""Public API of the AI module: the only thing other apps import from ``apps.ai``.

Everything here is safe to call when AI is off: ``is_available`` is False, reads return None, and
nothing is queued. AI proposes; people and the existing send path act.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def is_available(account) -> bool:
    """AI is configured for the site, the business's plan allows it, and the owner opted in."""
    from apps.ai.models import AISettings
    from apps.ai.providers import is_configured
    from apps.billing import api as billing_api

    if account is None or not is_configured():
        return False
    try:
        if not billing_api.module_enabled(account, "ai"):
            return False
    except KeyError:
        return False
    return AISettings.objects.filter(account=account, enabled=True).exists()


def _enqueue(proposal) -> None:
    from apps.ai.tasks import draft_proposal

    transaction.on_commit(lambda: draft_proposal.apply_async((proposal.pk,), countdown=3, queue="ai"))


def on_message_processed(sender, *, conversation, message, handled_by_automation: bool = False, **kwargs) -> None:
    """Receiver for ``conversations.signals.conversation_message_processed``.

    Queues one proposal per customer message, unless an automation already answered it or AI is
    off for this business. Idempotent: a replayed message reuses its proposal.
    """
    from apps.ai.models import AIProposal

    if handled_by_automation or message is None or message.direction != "inbound":
        return
    if not is_available(conversation.account):
        return
    proposal, created = AIProposal.objects.get_or_create(
        conversation=conversation, trigger_message=message,
        defaults={"account": conversation.account},
    )
    if created:
        _enqueue(proposal)


def request_proposal(account, conversation, user):
    """A person asked for a (new) suggestion. Replaces any live one for this conversation."""
    from apps.ai.models import AIProposal

    if not is_available(account):
        return None
    AIProposal.objects.filter(
        account=account, conversation=conversation,
        status__in=[AIProposal.Status.READY, AIProposal.Status.PENDING],
    ).update(status=AIProposal.Status.EXPIRED, expired_at=timezone.now())
    proposal = AIProposal.objects.create(account=account, conversation=conversation, requested_by=user)
    _enqueue(proposal)
    return proposal


def current_proposal(account, conversation):
    """The newest proposal worth showing (pending, ready or failed) for this conversation, or None."""
    from apps.ai.models import AIProposal

    return (
        AIProposal.objects.filter(
            account=account, conversation=conversation,
            status__in=[AIProposal.Status.PENDING, AIProposal.Status.READY, AIProposal.Status.ERROR],
        ).order_by("-created_at", "-id").first()
    )


def serialize(proposal, conversation) -> dict | None:
    """What the inbox card needs. Template variables are resolved for this customer, for review."""
    if proposal is None:
        return None
    out = {
        "id": proposal.pk, "status": proposal.status, "action": proposal.action,
        "reason": proposal.reason, "error": proposal.error, "created": proposal.created_at.isoformat(),
    }
    payload = proposal.payload or {}
    if proposal.action == "reply":
        out["text"] = payload.get("text", "")
    elif proposal.action == "send_template":
        from apps.automation import variables
        from apps.whatsapp import api as whatsapp_api

        template = whatsapp_api.approved_template_by_name(conversation.account, payload.get("template", ""))
        values = {}
        for blank, source in (payload.get("variables") or {}).items():
            value = variables.read_source(
                source, contact=conversation.contact, account=conversation.account, context={}) \
                if isinstance(source, str) else source
            values[blank] = value or ("there" if variables.looks_like_name(blank) else "")
        out.update({
            "template": payload.get("template", ""), "templateId": template.pk if template else None,
            "templateName": template.name if template else payload.get("template", ""), "values": values,
        })
    elif proposal.action == "handoff":
        out["note"] = payload.get("note", "")
    return out


def dismiss(account, proposal_id) -> bool:
    from apps.ai.models import AIProposal

    return bool(AIProposal.objects.filter(
        account=account, pk=proposal_id, status__in=[AIProposal.Status.READY, AIProposal.Status.ERROR],
    ).update(status=AIProposal.Status.DISMISSED, dismissed_at=timezone.now()))


def record_used(account, proposal_id, sent_text: str = "") -> None:
    """The team sent this proposal (or an edit of it). Best-effort: never blocks a send."""
    from apps.ai.models import AIProposal

    try:
        proposal = AIProposal.objects.filter(account=account, pk=int(proposal_id)).first()
    except (TypeError, ValueError):
        return
    if proposal is None or proposal.status != AIProposal.Status.READY:
        return
    edited = None
    if proposal.action == "reply":
        edited = " ".join((sent_text or "").split()) != " ".join((proposal.payload or {}).get("text", "").split())
    proposal.status, proposal.used_at, proposal.edited_before_send = AIProposal.Status.USED, timezone.now(), edited
    proposal.save(update_fields=["status", "used_at", "edited_before_send"])


# --- The business's AI settings ----------------------------------------------------------------
MAX_NOTES = 4000


def settings_for(account):
    from apps.ai.models import AISettings

    return AISettings.objects.filter(account=account).first() or AISettings(account=account)


def save_settings(account, user, *, enabled: bool, business_notes: str):
    """Turn AI suggestions on or off for a business. Turning it on records who agreed and when."""
    from apps.ai.models import AISettings

    ai_settings, _ = AISettings.objects.get_or_create(account=account)
    if enabled and not ai_settings.enabled:
        ai_settings.consented_at, ai_settings.consented_by = timezone.now(), user
    ai_settings.enabled = bool(enabled)
    ai_settings.business_notes = (business_notes or "").strip()[:MAX_NOTES]
    ai_settings.save()
    return ai_settings


def test_connection() -> dict:
    """``{"ok", "model", "latency_ms", "error"}`` for one tiny round trip. Sends no customer data."""
    import time

    from apps.ai.providers import AIProviderError, get_ai_provider

    started = time.monotonic()
    try:
        result = get_ai_provider().health()
    except AIProviderError as exc:
        return {"ok": False, "error": str(exc), "model": "", "latency_ms": None}
    return {"ok": True, "error": "", "model": result.model, "latency_ms": int((time.monotonic() - started) * 1000)}
