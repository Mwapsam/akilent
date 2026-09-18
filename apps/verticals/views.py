"""Vertical Starter packs gallery — "Templates should be the default way to
automate": a business owner activates a bundle of pre-built Workflows (and
the modules they need) in one click, instead of building them by hand.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.accounts.utils import get_current_account
from apps.verticals.registry import list_verticals
from apps.verticals.services import VerticalNotFound, VerticalTemplateInvalid, activate_vertical, activated_vertical_keys


@login_required
def templates(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    activated = activated_vertical_keys(account)
    return render(request, "verticals/templates.html", {
        "account": account,
        "verticals": list_verticals(),
        "activated": activated,
    })


@login_required
def activate(request, key: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if request.method != "POST":
        return redirect("verticals:templates")

    try:
        activate_vertical(account, key)
        messages.success(request, "Starter pack activated — its workflows are live.")
    except VerticalNotFound:
        messages.error(request, "That starter pack doesn't exist.")
    except VerticalTemplateInvalid as exc:
        messages.error(request, str(exc))

    return redirect("verticals:templates")
