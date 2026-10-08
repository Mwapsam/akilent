"""Settings, then AI: the owner's opt-in, the facts AI may use, and a connection test."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.ai import api as ai_api
from apps.ai import autonomy
from apps.ai.models import KnowledgeBaseEntry
from apps.ai.providers import is_configured


def _can_edit(request, account) -> bool:
    from apps.accounts import api as accounts_api

    return accounts_api.is_account_admin(request.user, account)


@login_required
def settings_ai(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    can_edit = _can_edit(request, account)
    if request.method == "POST":
        if not can_edit:
            messages.error(request, "Only an owner or admin can change AI settings.")
            return redirect("settings-ai")
        if request.POST.get("action") == "test":
            result = ai_api.test_connection()
            if result["ok"]:
                messages.success(
                    request,
                    f"Connected to {result['model']} in {result['latency_ms']} ms.",
                )
            else:
                messages.error(
                    request, f"Couldn't reach the AI service: {result['error']}"
                )
            return redirect("settings-ai")
        saved = ai_api.save_settings(
            account,
            request.user,
            enabled=request.POST.get("enabled") == "on",
            business_notes=request.POST.get("business_notes", ""),
            reply_mode=request.POST.get("reply_mode", "suggest"),
            auto_topics=request.POST.getlist("auto_topics"),
            auto_min_confidence=request.POST.get("auto_min_confidence"),
            auto_only_when_closed=request.POST.get("auto_only_when_closed") == "on",
            holding_reply_enabled=request.POST.get("holding_reply_enabled") == "on",
            holding_reply_text=request.POST.get("holding_reply_text"),
        )
        if saved.reply_mode == "auto" and not saved.auto_topics:
            messages.warning(
                request,
                "Saved. Tick at least one kind of question, or AI won't reply to anything on its own.",
            )
        else:
            messages.success(request, "AI settings saved.")
        return redirect("settings-ai")
    ai = ai_api.settings_for(account)
    locked = autonomy.locked_topics(account)
    return render(
        request,
        "accounts/settings_ai.html",
        {
            "account": account,
            "active_tab": "ai",
            "can_edit": can_edit,
            "site_configured": is_configured(),
            "ai": ai,
            "max_notes": ai_api.MAX_NOTES,
            "off_reason": ai_api.unavailable_reason(account),
            "website_hint": _website_hint(account),
            "topics": [
                {
                    "key": k,
                    "label": v,
                    "on": k in (ai.auto_topics or []) and k not in locked,
                    "locked": locked.get(k, ""),
                }
                for k, v in autonomy.TOPICS.items()
            ],
            "confidence_choices": autonomy.CONFIDENCE_CHOICES,
            "holding_reply": autonomy.holding_reply_text(ai),
            "autonomy_site_on": autonomy_site_on(),
            "auto_replies": ai_api.recent_auto_replies(account),
        },
    )


@login_required
def settings_ai_autopilot(request):
    """The autopilot report: what AI sent on its own, what it held back, and why."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        days = 7 if int(request.GET.get("days", 1)) == 7 else 1
    except ValueError:
        days = 1
    return render(
        request,
        "accounts/settings_ai_autopilot.html",
        {
            "account": account,
            "active_tab": "ai",
            "report": ai_api.autopilot_report(account, days=days),
            "ai": ai_api.settings_for(account),
        },
    )


@login_required
def settings_ai_knowledge(request):
    """FAQ/policy entries AI may answer from — see ``KnowledgeBaseEntry`` and
    ``apps.ai.facts.build``. Manually authored only, no document upload."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    can_edit = _can_edit(request, account)
    if request.method == "POST":
        if not can_edit:
            messages.error(request, "Only an owner or admin can change AI settings.")
            return redirect("settings-ai-knowledge")
        title = request.POST.get("title", "").strip()
        content = request.POST.get("content", "").strip()
        source_type = request.POST.get("source_type", KnowledgeBaseEntry.SourceType.FAQ)
        if not title or not content:
            messages.error(
                request, "A knowledge base entry needs both a title and content."
            )
        elif source_type not in KnowledgeBaseEntry.SourceType.values:
            messages.error(request, "That's not a valid entry type.")
        else:
            KnowledgeBaseEntry.objects.create(
                account=account, title=title, content=content, source_type=source_type
            )
            messages.success(request, "Added to the knowledge base.")
        return redirect("settings-ai-knowledge")
    entries = list(KnowledgeBaseEntry.objects.filter(account=account).order_by("title"))
    return render(
        request,
        "accounts/settings_ai_knowledge.html",
        {
            "account": account,
            "active_tab": "ai",
            "can_edit": can_edit,
            "entries": [e for e in entries if not e.needs_review],
            "review": [e for e in entries if e.needs_review],
            "source_types": KnowledgeBaseEntry.SourceType.choices,
            "off_reason": ai_api.unavailable_reason(account),
        },
    )


@login_required
@require_POST
def knowledge_entry_toggle(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if not _can_edit(request, account):
        messages.error(request, "Only an owner or admin can change AI settings.")
        return redirect("settings-ai-knowledge")
    entry = get_object_or_404(KnowledgeBaseEntry, account=account, pk=pk)
    if entry.needs_review:
        messages.error(request, "Check this entry and approve it first.")
        return redirect("settings-ai-knowledge")
    entry.is_active = not entry.is_active
    entry.save(update_fields=["is_active", "updated_at"])
    return redirect("settings-ai-knowledge")


def _website_hint(account) -> str:
    from apps.accounts import api as accounts_api

    site = (accounts_api.business_facts(account) or {}).get("website", "")
    return site if site.startswith(("http://", "https://")) else ""


def _knowledge_admin(request):
    """``(account, None)`` for someone who may change AI's knowledge, else ``(None, redirect)``."""
    account = get_current_account(request)
    if account is None:
        return None, redirect("dashboard")
    if not _can_edit(request, account):
        messages.error(request, "Only an owner or admin can change AI settings.")
        return None, redirect("settings-ai-knowledge")
    return account, None


@login_required
@require_POST
def knowledge_entry_edit(request, pk):
    """Save an entry's question and answer; ``approve`` also turns a reviewed entry on."""
    from django.utils import timezone

    account, stop = _knowledge_admin(request)
    if stop:
        return stop
    entry = get_object_or_404(KnowledgeBaseEntry, account=account, pk=pk)
    title = " ".join(request.POST.get("title", "").split())[:200]
    content = request.POST.get("content", "").strip()
    if not title or not content:
        messages.error(request, "An entry needs both a question and an answer.")
        return redirect("settings-ai-knowledge")
    entry.title, entry.content = title, content
    fields = ["title", "content", "updated_at"]
    if request.POST.get("approve"):
        entry.is_active, entry.reviewed_at = True, timezone.now()
        fields += ["is_active", "reviewed_at"]
        messages.success(request, "Approved. AI can answer from it now.")
    else:
        messages.success(request, "Saved.")
    entry.save(update_fields=fields)
    return redirect("settings-ai-knowledge")


@login_required
@require_POST
def knowledge_approve_all(request):
    """Approve every entry waiting for review that has an answer."""
    from django.utils import timezone

    account, stop = _knowledge_admin(request)
    if stop:
        return stop
    count = (
        KnowledgeBaseEntry.objects.filter(
            account=account,
            origin__in=KnowledgeBaseEntry.REVIEW_ORIGINS,
            reviewed_at__isnull=True,
        )
        .exclude(content="")
        .update(is_active=True, reviewed_at=timezone.now())
    )
    messages.success(request, f"Approved {count} entr{'y' if count == 1 else 'ies'}.")
    return redirect("settings-ai-knowledge")


MAX_IMPORT_FILE = 512 * 1024


@login_required
@require_POST
def knowledge_import(request):
    """Pasted or uploaded questions and answers, added under Needs review."""
    from apps.ai import knowledge

    account, stop = _knowledge_admin(request)
    if stop:
        return stop
    upload = request.FILES.get("file")
    if upload is not None:
        if upload.size > MAX_IMPORT_FILE:
            messages.error(request, "That file is too big. Keep it under 500 KB.")
            return redirect("settings-ai-knowledge")
        pairs = knowledge.parse_csv(upload.read())
    else:
        pairs = knowledge.parse_pasted(request.POST.get("text", ""))
    if not pairs:
        messages.error(
            request, "Couldn't find any questions in that. Put one per line."
        )
        return redirect("settings-ai-knowledge")
    out = knowledge.import_pairs(account, request.user, pairs)
    if not out["added"]:
        messages.info(
            request, "Your knowledge base already has all of those questions."
        )
    elif out["draft"] is not None:
        messages.success(
            request,
            f"Added {out['added']} for review. AI is drafting answers to {out['to_draft']} of "
            "them from what you've told it; refresh in a minute.",
        )
    elif out["to_draft"]:
        messages.success(
            request,
            f"Added {out['added']} for review. Write the answers to the "
            f"{out['to_draft']} questions without one.",
        )
    else:
        messages.success(request, f"Added {out['added']} for review.")
    return redirect("settings-ai-knowledge")


@login_required
@require_POST
def knowledge_website(request):
    """Read the business's website in the background; what it finds lands under Needs review."""
    from apps.ai import knowledge

    account, stop = _knowledge_admin(request)
    if stop:
        return stop
    try:
        url = knowledge.normalise_url(request.POST.get("url", ""))
    except knowledge.WebsiteError as exc:
        messages.error(request, str(exc))
        return redirect("settings-ai-knowledge")
    draft = ai_api.request_draft(account, request.user, "website", url, {"url": url})
    if draft is None:
        messages.error(request, "Turn AI on first (Settings, AI).")
    else:
        messages.success(
            request,
            "Reading your website. What AI finds will appear under Needs review in a minute "
            "or two; refresh to see it.",
        )
    return redirect("settings-ai-knowledge")


@login_required
@require_POST
def knowledge_entry_delete(request, pk):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if not _can_edit(request, account):
        messages.error(request, "Only an owner or admin can change AI settings.")
        return redirect("settings-ai-knowledge")
    entry = get_object_or_404(KnowledgeBaseEntry, account=account, pk=pk)
    entry.delete()
    messages.success(request, "Removed from the knowledge base.")
    return redirect("settings-ai-knowledge")


@login_required
@require_POST
def draft_create(request):
    """Queue an AI setup draft; the page polls ``draft_status``. JSON in and out."""
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"ok": False, "error": "no account"}, status=403)
    kind = request.POST.get("kind", "")
    if kind in ("knowledge_answers", "website"):
        # Started from the knowledge page, which checks the person may change AI's knowledge.
        return JsonResponse({"ok": False, "error": "Not available here."}, status=400)
    prompt = (request.POST.get("prompt") or "").strip()
    context = {}
    if kind == "automation":
        context["conversation"] = request.POST.get("conversation", "")
        if not prompt and not context["conversation"].strip():
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Describe what you want, or paste a conversation.",
                },
                status=400,
            )
        prompt = prompt or "Turn this conversation into an automation."
    elif kind == "template":
        if not prompt:
            return JsonResponse(
                {"ok": False, "error": "Describe the message you need."}, status=400
            )
    elif kind == "template_edit":
        context = {
            k: request.POST.get(k, "")
            for k in ("body", "instruction", "category", "language")
        }
        if not context["body"].strip():
            return JsonResponse(
                {"ok": False, "error": "Write the message first."}, status=400
            )
        prompt = context["instruction"]
    elif kind == "email_template":
        if not prompt:
            return JsonResponse(
                {"ok": False, "error": "Describe the email you need."}, status=400
            )
    elif kind == "playground":
        if not prompt:
            return JsonResponse(
                {"ok": False, "error": "Type a question a customer might ask."},
                status=400,
            )
        context = {"channel": request.POST.get("channel", "whatsapp")}
    elif kind == "email_edit":
        context, error = _email_edit_context(request)
        if error:
            return JsonResponse({"ok": False, "error": error}, status=400)
        prompt = context["instruction"]
    draft = ai_api.request_draft(account, request.user, kind, prompt, context)
    if draft is None:
        return JsonResponse(
            {
                "ok": False,
                "error": "AI isn't switched on for your business (Settings, AI).",
            },
            status=400,
        )
    return JsonResponse({"ok": True, "draft": ai_api.draft_json(draft)})


def _email_edit_context(request) -> tuple[dict, str]:
    """The parts to rewrite (checked again: they come from the browser), or a saved email's words
    for subject lines."""
    import json

    from apps.ai.drafting import SUBJECTS
    from apps.email import ai_layout

    instruction = request.POST.get("instruction", "")
    raw = request.POST.get("parts", "")
    if raw:
        try:
            parts = ai_layout.clean_parts(json.loads(raw))
        except (ValueError, TypeError):
            return {}, "Draft the email first."
        return {"instruction": instruction, "parts": parts}, ""
    if instruction != SUBJECTS:
        return {}, "Draft the email first."
    context = {
        k: request.POST.get(k, "")[:20000]
        for k in ("subject", "text_body", "html_body")
    }
    if not (context["subject"].strip() or context["text_body"].strip()):
        return {}, "Write the email first."
    return {"instruction": instruction, **context}, ""


@login_required
def draft_status(request, pk: int):
    account = get_current_account(request)
    draft = ai_api.get_draft(account, pk) if account else None
    if draft is None:
        return JsonResponse({"ok": False, "error": "not found"}, status=404)
    return JsonResponse({"ok": True, "draft": ai_api.draft_json(draft)})


def autonomy_site_on() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "AI_AUTONOMY_ENABLED", True))
