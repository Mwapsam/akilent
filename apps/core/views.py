"""Public pages served by core: help center, legal pages, developer docs and the health check.

The Operator Console (/manage/) lives in apps/core/console/.
"""
import logging

from django.http import Http404
from django.shortcuts import render

from apps.core import docs as docs_kb
from apps.core import help as help_kb

logger = logging.getLogger(__name__)


def healthz(request):
    """Liveness for the container health check, the deploy verification and uptime monitors.

    Public and cheap. The database decides the answer (503 without it); the cache is reported but
    doesn't fail it, since the site still works without Redis. Celery isn't checked here: a
    stopped worker must not make Docker restart a healthy web container (the Pilot Command Center
    shows workers).
    """
    from django.core.cache import cache
    from django.db import connection
    from django.http import JsonResponse

    status = {"ok": True, "db": True, "cache": True}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        logger.exception("healthz: database check failed")
        status["ok"] = status["db"] = False
    try:
        cache.set("healthz", "1", 10)
        status["cache"] = cache.get("healthz") == "1"
    except Exception:
        status["cache"] = False
    return JsonResponse(status, status=200 if status["ok"] else 503)


# --- Help center (public knowledge base) --------------------------------------

def help_index(request):
    q = (request.GET.get("q") or "").strip()
    results = help_kb.search(q) if q else None
    return render(request, "help/index.html", {
        "q": q,
        "results": results,
        "categories": help_kb.grouped(),
    })


def help_article(request, slug):
    article = help_kb.get_article(slug)
    if article is None:
        raise Http404("No such help article")
    return render(request, "help/article.html", {
        "article": article,
        "related": help_kb.related_to(article),
    })


# --- Legal pages (public) ------------------------------------------------------

_LEGAL_PAGES = {
    "privacy": ("legal/privacy.html", "Privacy policy"),
    "terms": ("legal/terms.html", "Terms of service"),
    "data-deletion": ("legal/data_deletion.html", "Data deletion"),
}
_LEGAL_UPDATED = "26 September 2026"  # change with the text


def legal_page(request, slug):
    template, title = _LEGAL_PAGES[slug]
    return render(request, template, {"page_title": title, "updated": _LEGAL_UPDATED})


# --- Developer docs (public) ---------------------------------------------------

def docs_page(request, slug="index"):
    from django.conf import settings

    page = docs_kb.get_page(slug)
    if page is None:
        raise Http404("No such docs page")
    prev_page, next_page = docs_kb.neighbors(page)
    return render(request, page.template, {
        "page": page,
        "pages": docs_kb.PAGES,
        "prev_page": prev_page,
        "next_page": next_page,
        "smtp_relay_host": settings.SMTP_RELAY_HOST,
        "smtp_relay_port": settings.SMTP_RELAY_PORT,
    })
