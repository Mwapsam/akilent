"""Public pages served by core: help center, legal pages, developer docs and the health check.

The Operator Console (/manage/) lives in apps/core/console/.
UX event ingest endpoint (/internal/ux-event/) also lives here.
"""

import json
import logging

from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.core import docs as docs_kb
from apps.core import help as help_kb

logger = logging.getLogger(__name__)

_UX_TYPES = {
    "rage_click",
    "dead_click",
    "rapid_navigation",
    "repeated_form_failure",
    "js_error",
}
_RATE_LIMIT_KEY = "ux_event_rate:{sid}"
_RATE_LIMIT_MAX = 60  # events per minute per session


@require_POST
def ingest_ux_event(request) -> JsonResponse:
    """Receive a UX friction / JS error event from the browser tracker.

    - Session ID is derived from the middleware-set request attribute, never
      the POST body (the body value is only used as a cross-check).
    - Payload size limits are enforced here.
    - Rate-limited to 60 events/minute per session.
    - No authentication required — anonymous sessions are valid.
    """
    sid = getattr(request, "browser_session_id", "")
    if not sid:
        return JsonResponse({"ok": False, "error": "no_session"}, status=400)

    # Rate-limit
    from django.core.cache import cache

    rate_key = _RATE_LIMIT_KEY.format(sid=sid)
    count = cache.get(rate_key, 0)
    if count >= _RATE_LIMIT_MAX:
        return JsonResponse({"ok": False, "error": "rate_limited"}, status=429)
    cache.set(rate_key, count + 1, timeout=60)

    try:
        payload = json.loads(request.body[:65_536])  # hard cap on body size
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)

    event_type = str(payload.get("type", ""))[:40]
    if event_type not in _UX_TYPES:
        return JsonResponse({"ok": False, "error": "unknown_type"}, status=400)

    page = str(payload.get("page", ""))[:255]
    target = str(payload.get("target", ""))[:255]
    click_count = payload.get("click_count")
    duration_ms = payload.get("duration_ms")
    request_id = str(payload.get("request_id", ""))[:64]

    raw_data = payload.get("data", {})
    if not isinstance(raw_data, dict):
        raw_data = {}
    # Enforce 16 KB limit on the data blob
    data_str = json.dumps(raw_data)
    if len(data_str) > 16_384:
        raw_data = {"truncated": True, "message": data_str[:2048]}

    try:
        from apps.logs.models import BrowserSession, UxEvent

        try:
            session = BrowserSession.objects.get(session_id=sid)
        except BrowserSession.DoesNotExist:
            return JsonResponse({"ok": False, "error": "session_not_found"}, status=404)

        account = getattr(request, "account", None) or session.account

        UxEvent.objects.create(
            session=session,
            account=account,
            type=event_type,
            page=page,
            target=target,
            click_count=int(click_count)
            if isinstance(click_count, (int, float))
            else None,
            duration_ms=int(duration_ms)
            if isinstance(duration_ms, (int, float))
            else None,
            data=raw_data,
            request_id=request_id,
        )
    except Exception:
        logger.exception("ingest_ux_event failed for sid=%s", sid)
        return JsonResponse({"ok": False, "error": "server_error"}, status=500)

    return JsonResponse({"ok": True})


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
