"""Bulk import from CSV (apps.publishing.csv_import). Behind the ``cs_csv_import``
release gate: until it is released, ReleaseGateMiddleware 404s these URLs.

The preview keeps the file's rows in the session; importing reads them again from
there and checks every row once more before saving it as a draft. Each row has a key,
so a repeated import never duplicates a draft."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from apps.accounts.utils import get_current_account
from apps.core.module_gate import module_required

from . import csv_import
from .views import _in_workspace_tz

_SESSION_KEY = "publishing_csv_import"


@login_required
@module_required("content_studio")
@_in_workspace_tz
def csv_import_page(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if request.method == "POST" and request.POST.get("action") == "import":
        stored = request.session.get(_SESSION_KEY)
        if not stored or stored.get("account") != account.pk:
            messages.error(request, "Choose the file again.")
            return redirect("publishing:csv-import")
        # Rows carry their own keys, so if this request dies partway, importing again
        # makes only the drafts still missing. The rows leave the session once done.
        result = csv_import.import_rows(account, request.user, stored["rows"])
        request.session.pop(_SESSION_KEY, None)
        created = len(result["created"])
        messages.success(
            request,
            f"Imported {created} draft{'s' if created != 1 else ''}. Open each one to "
            "check it and schedule it.",
        )
        for n, why in result["failed"]:
            messages.error(request, f"Row {n} wasn't imported: {why}")
        return redirect("publishing:library")

    preview = None
    if request.method == "POST":
        try:
            rows = csv_import.read_file(request.FILES.get("file"))
        except csv_import.CsvError as exc:
            messages.error(request, str(exc))
            return redirect("publishing:csv-import")
        preview = csv_import.preview(account, rows)
        request.session[_SESSION_KEY] = {"account": account.pk, "rows": rows}

    return render(
        request,
        "publishing/csv_import.html",
        {
            "account": account,
            "preview": preview,
            "ready": sum(1 for r in preview or [] if r["ok"]),
            "max_rows": csv_import.MAX_ROWS,
            "columns": csv_import.COLUMNS,
        },
    )
