import json

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

    content_signal = "Content-Signal: ai-train=no, search=yes, ai-input=no\n"

    if not allow_indexing:
        body = f"User-agent: *\nDisallow: /\n\n{content_signal}"
    else:
        disallows = "\n".join(f"Disallow: {p}" for p in _PRIVATE_PATHS)
        body = (
            f"User-agent: *\nAllow: /\n\n{disallows}\n\n"
            f"Sitemap: {site_url}/sitemap.xml\n\n{content_signal}"
        )

    return HttpResponse(body, content_type="text/plain")


def api_catalog(request):
    """RFC 9727 API catalog at /.well-known/api-catalog.

    Enables automated discovery of the platform's APIs by agents and tooling
    that follow https://www.rfc-editor.org/rfc/rfc9727.
    """
    site_url = getattr(settings, "SITE_URL", "https://akilent.com").rstrip("/")
    catalog = {
        "linkset": [
            {
                "anchor": f"{site_url}/api/",
                "service-desc": [
                    {
                        "href": f"{site_url}/api/schema",
                        "type": "application/vnd.oai.openapi+json;version=3.0",
                    }
                ],
                "service-doc": [
                    {
                        "href": f"{site_url}/docs/",
                        "type": "text/html",
                    }
                ],
                "status": [
                    {
                        "href": f"{site_url}/healthz",
                        "type": "application/json",
                    }
                ],
            }
        ]
    }
    return HttpResponse(
        json.dumps(catalog),
        content_type="application/linkset+json",
    )
