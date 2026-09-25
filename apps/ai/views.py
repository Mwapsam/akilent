"""Settings, then AI: the owner's opt-in, the facts AI may use, and a connection test."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.accounts.utils import get_current_account
from apps.ai import api as ai_api
from apps.ai import autonomy
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
                messages.success(request, f"Connected to {result['model']} in {result['latency_ms']} ms.")
            else:
                messages.error(request, f"Couldn't reach the AI service: {result['error']}")
            return redirect("settings-ai")
        saved = ai_api.save_settings(
            account, request.user, enabled=request.POST.get("enabled") == "on",
            business_notes=request.POST.get("business_notes", ""),
            reply_mode=request.POST.get("reply_mode", "suggest"),
            auto_topics=request.POST.getlist("auto_topics"),
            auto_min_confidence=request.POST.get("auto_min_confidence"),
            auto_only_when_closed=request.POST.get("auto_only_when_closed") == "on",
        )
        if saved.reply_mode == "auto" and not saved.auto_topics:
            messages.warning(request, "Saved. Tick at least one kind of question, or AI won't reply to anything on its own.")
        else:
            messages.success(request, "AI settings saved.")
        return redirect("settings-ai")
    ai = ai_api.settings_for(account)
    locked = autonomy.locked_topics(account)
    return render(request, "accounts/settings_ai.html", {
        "account": account, "active_tab": "ai", "can_edit": can_edit,
        "site_configured": is_configured(), "ai": ai,
        "max_notes": ai_api.MAX_NOTES, "off_reason": ai_api.unavailable_reason(account),
        "is_staff": request.user.is_staff,
        "topics": [{"key": k, "label": v, "on": k in (ai.auto_topics or []) and k not in locked,
                    "locked": locked.get(k, "")} for k, v in autonomy.TOPICS.items()],
        "confidence_choices": autonomy.CONFIDENCE_CHOICES,
        "autonomy_site_on": autonomy_site_on(),
        "auto_replies": ai_api.recent_auto_replies(account),
    })


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
    return render(request, "accounts/settings_ai_autopilot.html", {
        "account": account, "active_tab": "ai", "report": ai_api.autopilot_report(account, days=days),
        "ai": ai_api.settings_for(account),
    })


def autonomy_site_on() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "AI_AUTONOMY_ENABLED", True))
