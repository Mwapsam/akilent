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

    from apps.core.tasks import BEAT_HEARTBEAT_KEY

    status = {"ok": True, "db": True, "cache": True, "beat": True}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        logger.exception("healthz: database check failed")
        status["ok"] = status["db"] = False
    try:
        cache.set("healthz", "1", 10)
        status["cache"] = cache.get("healthz") == "1"
        # beat is informational — a stopped beat must not restart the web container,
        # so it does not affect status["ok"]. Uptime monitors check for "beat": true.
        status["beat"] = bool(cache.get(BEAT_HEARTBEAT_KEY))
    except Exception:
        status["cache"] = False
        status["beat"] = False
    return JsonResponse(status, status=200 if status["ok"] else 503)


# --- Help center (public knowledge base) --------------------------------------


def help_index(request):
    q = (request.GET.get("q") or "").strip()
    results = help_kb.search(q) if q else None
    categories = help_kb.grouped()

    from apps.core.markdown import wants_markdown

    if wants_markdown(request):
        from apps.core.markdown import markdown_response

        lines = ["# Help Center\n"]
        if q and results is not None:
            lines.append(f'**Search results for "{q}"**\n')
            for a in results:
                lines.append(f"- [{a.title}](/help/{a.slug}/) — {a.summary}")
        else:
            for cat, articles in categories:
                lines.append(f"\n## {cat}\n")
                for a in articles:
                    lines.append(f"- [{a.title}](/help/{a.slug}/) — {a.summary}")
        return markdown_response("\n".join(lines))

    return render(
        request,
        "help/index.html",
        {
            "q": q,
            "results": results,
            "categories": categories,
        },
    )


def help_article(request, slug):
    article = help_kb.get_article(slug)
    if article is None:
        raise Http404("No such help article")
    related = help_kb.related_to(article)

    from apps.core.markdown import wants_markdown

    if wants_markdown(request):
        from django.template.loader import render_to_string

        from apps.core.markdown import html_to_markdown, markdown_response

        body_html = render_to_string(
            article.template, {"article": article}, request=request
        )
        body_md = html_to_markdown(body_html)
        related_md = ""
        if related:
            related_md = "\n\n## Related guides\n\n" + "\n".join(
                f"- [{a.title}](/help/{a.slug}/)" for a in related
            )
        text = (
            f"# {article.title}\n\n"
            f"{article.summary}\n\n"
            f"**Category:** {article.category}\n\n"
            f"---\n\n"
            f"{body_md}"
            f"{related_md}"
        )
        return markdown_response(text)

    return render(
        request,
        "help/article.html",
        {
            "article": article,
            "related": related,
        },
    )


# --- Legal pages (public) ------------------------------------------------------

_LEGAL_PAGES = {
    "privacy": ("legal/privacy.html", "Privacy policy"),
    "terms": ("legal/terms.html", "Terms of service"),
    "data-deletion": ("legal/data_deletion.html", "Data deletion"),
    "cookies": ("legal/cookies.html", "Cookie policy"),
}
_LEGAL_UPDATED = "26 September 2026"  # change with the text


def legal_page(request, slug):
    template, title = _LEGAL_PAGES[slug]
    ctx = {"page_title": title, "updated": _LEGAL_UPDATED}

    from apps.core.markdown import wants_markdown

    if wants_markdown(request):
        from django.template.loader import render_to_string

        from apps.core.markdown import extract_and_convert, markdown_response

        html = render_to_string(template, ctx, request=request)
        body_md = extract_and_convert(html)
        text = f"# {title}\n\n_Last updated {_LEGAL_UPDATED}_\n\n{body_md}"
        return markdown_response(text)

    return render(request, template, ctx)


# --- Developer docs (public) ---------------------------------------------------


def docs_page(request, slug="index"):
    from django.conf import settings

    page = docs_kb.get_page(slug)
    if page is None:
        raise Http404("No such docs page")
    prev_page, next_page = docs_kb.neighbors(page)
    ctx = {
        "page": page,
        "pages": docs_kb.PAGES,
        "prev_page": prev_page,
        "next_page": next_page,
        "smtp_relay_host": settings.SMTP_RELAY_HOST,
        "smtp_relay_port": settings.SMTP_RELAY_PORT,
    }

    from apps.core.markdown import wants_markdown

    if wants_markdown(request):
        from django.template.loader import render_to_string

        from apps.core.markdown import extract_and_convert, markdown_response

        html = render_to_string(page.template, ctx, request=request)
        body_md = extract_and_convert(html)
        nav_md = ""
        if prev_page:
            nav_md += f"\n\n← [Previous: {prev_page.title}](/docs/{prev_page.slug}/)"
        if next_page:
            nav_md += f"\n\n→ [Next: {next_page.title}](/docs/{next_page.slug}/)"
        text = (
            f"# {page.title}\n\n"
            f"{page.summary}\n\n"
            f"**Section:** {page.section}\n\n"
            f"---\n\n"
            f"{body_md}"
            f"{nav_md}"
        )
        return markdown_response(text)

    return render(request, page.template, ctx)
