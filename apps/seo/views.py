from django.conf import settings
from django.http import HttpResponse

_PRIVATE_PATHS = [
    "/admin/",
    "/api/",
    "/auth/",
    "/dashboard/",
    "/inbox/",
    "/contacts/",
    "/sales/",
    "/orders/",
    "/email/",
    "/ai/",
    "/automations/",
    "/build/",
    "/billing/",
    "/scheduled/",
    "/manage/",
]


def robots_txt(request):
    site_url = getattr(settings, "SITE_URL", "https://akilent.com").rstrip("/")
    allow_indexing = getattr(settings, "SEO_ALLOW_INDEXING", False)

    if not allow_indexing:
        body = "User-agent: *\nDisallow: /\n"
    else:
        disallows = "\n".join(f"Disallow: {p}" for p in _PRIVATE_PATHS)
        body = f"User-agent: *\nAllow: /\n\n{disallows}\n\nSitemap: {site_url}/sitemap.xml\n"

    return HttpResponse(body, content_type="text/plain")
