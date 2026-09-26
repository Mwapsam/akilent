"""Public API of the AI module: the only thing other apps import from ``apps.ai``.

Everything here is safe to call when AI is off: ``is_available`` is False, reads return None, and
nothing is queued. AI proposes; people and the existing send path act.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def unavailable_reason(account) -> str | None:
    """Why AI is off for this business, in plain words, or None when it is on.

    Three switches, checked in order: the site's provider, the business's plan (the AI module),
    and the owner's opt-in.
    """
    from apps.ai.models import AISettings
    from apps.ai.providers import is_configured
    from apps.billing import api as billing_api

    if account is None:
        return "No business selected."
    if not account.is_active:
        return "This business is suspended."
    if not is_configured():
        return "AI isn't set up on this Akilent installation yet."
    try:
        module_on = billing_api.module_enabled(account, "ai")
    except KeyError:
        module_on = False
    if not module_on:
        return "The AI module is switched off for this business's plan."
    if not AISettings.objects.filter(account=account, enabled=True).exists():
        return "AI suggestions aren't turned on below."
    return None


def is_available(account) -> bool:
    """AI is configured for the site, the business's plan allows it, and the owner opted in."""
    return unavailable_reason(account) is None


def seed_notes(account, text: str) -> bool:
    """Start the AI notes from the owner's profile answers, only if they haven't written any yet.
    Doesn't switch AI on. Returns whether the notes were filled."""
    from apps.ai.models import AISettings

    text = (text or "").strip()
    if not text:
        return False
    ai_settings, _ = AISettings.objects.get_or_create(account=account)
    if ai_settings.business_notes.strip():
        return False
    ai_settings.business_notes = text[:MAX_NOTES]
    ai_settings.save(update_fields=["business_notes", "updated_at"])
    return True


DRAFT_KINDS = ("automation", "template", "template_edit", "email_template", "email_edit")


def request_draft(account, user, kind: str, prompt: str, context: dict | None = None):
    """Queue an AI setup draft. Returns the ``AIDraft``, or None when AI is off for this business."""
    from apps.ai import drafting
    from apps.ai.models import AIDraft
    from apps.ai.tasks import build_draft

    if kind not in DRAFT_KINDS or not is_available(account):
        return None
    context = dict(context or {})
    if context.get("conversation"):
        context["conversation"] = drafting.clean_prompt(context["conversation"])
    draft = AIDraft.objects.create(account=account, kind=kind, prompt=drafting.clean_prompt(prompt),
                                   context=context, created_by=user if getattr(user, "pk", None) else None)
    transaction.on_commit(lambda: build_draft.apply_async((draft.pk,), queue="ai"))
    return draft


def get_draft(account, draft_id, kind: str | None = None):
    from apps.ai.models import AIDraft

    try:
        qs = AIDraft.objects.filter(account=account, pk=int(draft_id))
    except (TypeError, ValueError):
        return None
    return (qs.filter(kind=kind) if kind else qs).first()


def draft_json(draft) -> dict | None:
    from apps.ai import drafting

    return drafting.as_json(draft) if draft else None


def mark_draft_used(draft) -> None:
    from apps.ai.models import AIDraft

    if draft is not None and draft.status == AIDraft.Status.READY:
        draft.status, draft.used_at = AIDraft.Status.USED, timezone.now()
        draft.save(update_fields=["status", "used_at"])


def unchecked_facts(account, text: str) -> list[str]:
    """Prices, times, numbers and links in ``text`` that can't be traced to the business's own facts
    (catalogue, opening hours, profile answers, AI notes). Deterministic: works with AI off too, so
    automation drafts can warn "K5,000 isn't in your notes"."""
    from apps.ai import autonomy
    from apps.ai import facts as business_facts
    from apps.ai.models import AISettings

    notes = AISettings.objects.filter(account=account).values_list("business_notes", flat=True).first() or ""
    return autonomy.unsupported_facts(text, business_facts.build(account, business_notes=notes))


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
        # Business on automatic replies, but this one wasn't sent: say which check held it back.
        "autoNote": _auto_note(proposal.auto_decision),
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
    out["extras"] = [
        {"index": i, "kind": e.get("kind"), "label": extra_label(e), "applied": bool(e.get("applied_at"))}
        for i, e in enumerate(proposal.extras or [])
    ]
    out["lookups"] = [LOOKUP_LABELS.get(name, name) for name in (proposal.tools_used or [])]
    return out


def _auto_note(decision: dict | None) -> str:
    if not decision or decision.get("send"):
        return ""
    if decision.get("error"):
        return f"Not sent automatically: {decision['error']}"
    failed = next((c for c in decision.get("checks", []) if not c.get("ok")), None)
    if failed is None or failed.get("name") == "switched_on":
        return ""
    return f"Not sent automatically: {failed.get('detail', '')}"


LOOKUP_LABELS = {
    "check_opening_hours": "opening hours", "search_products": "your products", "get_customer": "customer history",
}


def extra_label(extra: dict) -> str:
    kind = extra.get("kind")
    if kind == "tag":
        return f"Tag “{extra.get('tag', '')}”"
    if kind == "track_interest":
        return "Track as interested"
    if kind == "follow_up":
        days = int(extra.get("in_days") or 1)
        when = "tomorrow" if days == 1 else f"in {days} days"
        return f"Follow up {when}" + (f": {extra['note']}" if extra.get("note") else "")
    return kind or ""


def apply_extra(account, conversation, proposal_id, index, user) -> tuple[bool, str]:
    """A person clicked one of a proposal's side suggestions. Runs it through the Action Registry.

    ``(ok, message)``; the message is shown to the person. Applying twice does nothing the second time.
    """
    from datetime import timedelta

    from apps.ai.models import AIProposal
    from apps.core.actions import ActionError, run_action

    try:
        proposal = AIProposal.objects.select_related("conversation__contact").filter(
            account=account, conversation=conversation, pk=int(proposal_id)).first()
        index = int(index)
    except (TypeError, ValueError):
        return False, "That suggestion wasn't found."
    extras = list(proposal.extras or []) if proposal else []
    if proposal is None or proposal.conversation is None or not 0 <= index < len(extras):
        return False, "That suggestion wasn't found."
    if proposal.status not in (AIProposal.Status.READY, AIProposal.Status.USED):
        return False, "That suggestion is out of date."
    extra = extras[index]
    if extra.get("applied_at"):
        return True, "Already done."
    conversation, ctx = proposal.conversation, {"account": account}
    try:
        if extra["kind"] == "tag":
            run_action("add_tag", ctx, contact=conversation.contact, tag=extra["tag"])
            done = f"Tagged “{extra['tag']}”."
        elif extra["kind"] == "track_interest":
            run_action("capture_conversation_lead", ctx, account=account, contact=conversation.contact,
                       conversation_id=conversation.public_id, signal="AI suggestion", owner=user)
            done = "Tracked as interested."
        elif extra["kind"] == "follow_up":
            run_action("create_followup", ctx, conversation=conversation,
                       due_at=timezone.now() + timedelta(days=int(extra.get("in_days") or 1)),
                       note=extra.get("note", ""), created_by=user)
            done = "Follow-up scheduled."
        else:
            return False, "That suggestion wasn't found."
    except ActionError as exc:
        return False, str(exc)
    extras[index] = dict(extra, applied_at=timezone.now().isoformat())
    proposal.extras = extras
    proposal.save(update_fields=["extras"])
    return True, done


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


def save_settings(account, user, *, enabled: bool, business_notes: str, reply_mode: str | None = None,
                  auto_topics=None, auto_min_confidence=None, auto_only_when_closed: bool = False):
    """Turn AI on or off for a business, and choose between suggestions and automatic replies.

    Turning AI on, and separately choosing automatic replies, each record who agreed and when.
    ``reply_mode=None`` leaves the reply settings as they are.
    """
    from apps.ai import autonomy
    from apps.ai.models import AISettings

    ai_settings, _ = AISettings.objects.get_or_create(account=account)
    if enabled and not ai_settings.enabled:
        ai_settings.consented_at, ai_settings.consented_by = timezone.now(), user
    ai_settings.enabled = bool(enabled)
    ai_settings.business_notes = (business_notes or "").strip()[:MAX_NOTES]
    if reply_mode is not None:
        mode = reply_mode if reply_mode in AISettings.ReplyMode.values else AISettings.ReplyMode.SUGGEST
        if mode == AISettings.ReplyMode.AUTO and ai_settings.reply_mode != mode:
            ai_settings.auto_consented_at, ai_settings.auto_consented_by = timezone.now(), user
        ai_settings.reply_mode = mode
        locked = autonomy.locked_topics(account)
        ai_settings.auto_topics = [t for t in autonomy.TOPICS if t in set(auto_topics or []) and t not in locked]
        allowed = {value for value, _label in autonomy.CONFIDENCE_CHOICES}
        try:
            confidence = float(auto_min_confidence)
        except (TypeError, ValueError):
            confidence = ai_settings.auto_min_confidence
        ai_settings.auto_min_confidence = confidence if confidence in allowed else 0.85
        ai_settings.auto_only_when_closed = bool(auto_only_when_closed)
    ai_settings.save()
    return ai_settings


def autopilot_report(account, *, days: int = 1, limit: int = 50) -> dict:
    """What autopilot decided over the last ``days``: counts, why replies were held, and each decision.

    Built from the audit trail stored on every proposal (``auto_decision``), so a row can always
    answer "why did AI reply at 7:42?" and "why didn't it reply?".
    """
    from datetime import timedelta

    from apps.ai import autonomy
    from apps.ai.models import AIProposal

    since = timezone.now() - timedelta(days=days)
    decided = (AIProposal.objects.filter(account=account, created_at__gte=since)
               .exclude(auto_decision={}).select_related("conversation__contact").order_by("-created_at"))
    sent = held = reviewed = 0
    reasons: dict[str, int] = {}
    rows = []
    for p in decided:
        decision = p.auto_decision or {}
        failed = next((c for c in decision.get("checks", []) if not c.get("ok")), None)
        if decision.get("send"):
            sent += 1
            outcome = "Sent by AI"
        else:
            held += 1
            reason = decision.get("error") and "Sending failed" or autonomy.HELD_REASONS.get(
                (failed or {}).get("name"), "Other")
            reasons[reason] = reasons.get(reason, 0) + 1
            if p.status == AIProposal.Status.USED:
                reviewed += 1
            outcome = "Held: " + reason
        if len(rows) < limit:
            rows.append({
                "at": p.auto_sent_at or p.ready_at or p.created_at, "outcome": outcome, "sent": bool(decision.get("send")),
                "status": p.get_status_display(), "text": (p.payload or {}).get("text", "") or (p.payload or {}).get("note", ""),
                "customer": (p.conversation.contact.first_name or "A customer") if p.conversation else "A customer",
                "conversation_id": p.conversation.public_id if p.conversation else "",
                "checks": decision.get("checks", []), "error": decision.get("error", ""),
            })
    return {
        "days": days, "sent": sent, "held": held, "reviewed": reviewed,
        "reasons": sorted(reasons.items(), key=lambda kv: -kv[1]), "rows": rows,
    }


def recent_auto_replies(account, limit: int = 20) -> list[dict]:
    """The latest replies AI sent on its own, newest first, for the owner to review."""
    from apps.ai import autonomy
    from apps.ai.models import AIProposal

    rows = (AIProposal.objects.filter(account=account, auto_sent_at__isnull=False)
            .select_related("conversation__contact").order_by("-auto_sent_at")[:limit])
    return [{
        "sent_at": p.auto_sent_at, "text": (p.payload or {}).get("text", ""),
        "topic": autonomy.TOPICS.get(p.intent, p.intent), "confidence": p.confidence,
        "customer": (p.conversation.contact.first_name or "A customer") if p.conversation else "A customer",
        "conversation_id": p.conversation.public_id if p.conversation else "",
    } for p in rows]


def usage_summary(account=None, *, since) -> dict:
    """``{"suggested", "used", "errors"}`` since ``since``: does the team trust the suggestions?

    ``account=None`` covers every business (the staff view). "Suggested" counts proposals the model
    actually produced (``ready_at`` set), whatever happened next. An EXPIRED status alone isn't
    enough: a proposal overtaken while still waiting on the model is expired too, never shown.
    "Used" is the team sending one.
    """
    from apps.ai.models import AIProposal

    rows = AIProposal.objects.filter(created_at__gte=since)
    if account is not None:
        rows = rows.filter(account=account)
    status = AIProposal.Status
    return {
        "suggested": rows.filter(ready_at__isnull=False).count(),
        "used": rows.filter(status=status.USED).count(),
        "errors": rows.filter(status=status.ERROR).count(),
    }


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
