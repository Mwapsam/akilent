"""Settings, then AI: the owner's opt-in, the facts AI may use, and a connection test."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.accounts.utils import get_current_account
from apps.ai import api as ai_api
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
        ai_api.save_settings(
            account, request.user, enabled=request.POST.get("enabled") == "on",
            business_notes=request.POST.get("business_notes", ""),
        )
        messages.success(request, "AI settings saved.")
        return redirect("settings-ai")
    return render(request, "accounts/settings_ai.html", {
        "account": account, "active_tab": "ai", "can_edit": can_edit,
        "site_configured": is_configured(), "ai": ai_api.settings_for(account),
        "max_notes": ai_api.MAX_NOTES,
    })
