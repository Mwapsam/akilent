import json

from django.conf import settings
from django.templatetags.static import static

# Paths that always get noindex regardless of any DB override.
_PRIVATE_PREFIXES = (
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
    "/healthz",
    "/events/",
)


def _is_private(path):
    return any(path.startswith(p) for p in _PRIVATE_PREFIXES)


def _build_canonical(request):
    """Absolute canonical URL: SITE_URL + path, no query string or fragment."""
    site_url = getattr(settings, "SITE_URL", "").rstrip("/")
    return f"{site_url}{request.path}"


def _resolve_og_image(relative_path):
    """Turn a relative static path into an absolute URL."""
    site_url = getattr(settings, "SITE_URL", "").rstrip("/")
    try:
        url = static(relative_path)
        if url.startswith("http"):
            return url
        return f"{site_url}{url}"
    except Exception:
        return ""


def _default_structured_data():
    site_url = getattr(settings, "SITE_URL", "https://akilent.com").rstrip("/") + "/"
    return [
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "Akilent",
            "url": site_url,
        },
        {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": "Akilent",
            "url": site_url,
        },
    ]


def resolve_seo_context(request):
    defaults = getattr(settings, "SEO_DEFAULTS", {})
    site_name = defaults.get("site_name", "Akilent")
    default_title = defaults.get("default_title", site_name)
    title_suffix = defaults.get("title_suffix", f"— {site_name}")
    default_og_image_path = defaults.get("og_image", "img/seo/og-default.png")

    # Resolution order (highest wins): global noindex → private route → DB override → request context → defaults
    global_noindex = not getattr(settings, "SEO_ALLOW_INDEXING", False)
    private = _is_private(request.path)

    # DB override (optional, best-effort)
    db_override = None
    if not private:
        try:
            from .models import SEOPage

            db_override = SEOPage.objects.filter(path=request.path).first()
        except Exception:
            pass

    # If private route, noindex wins unconditionally — DB override cannot unset it.
    if global_noindex or private or db_override and db_override.noindex:
        index = follow = False
    else:
        index = follow = True

    # Merge title
    page_title = (db_override.title if db_override else "") or ""
    if page_title and page_title != site_name:
        title = f"{page_title} {title_suffix}"
    else:
        title = f"{default_title} {title_suffix}"

    description = (db_override.description if db_override else "") or defaults.get(
        "default_description", ""
    )
    canonical_url = (
        db_override.canonical_url if db_override else ""
    ) or _build_canonical(request)

    if db_override and db_override.og_image:
        og_image = request.build_absolute_uri(db_override.og_image.url)
    else:
        og_image = _resolve_og_image(default_og_image_path)

    structured_data = _default_structured_data()

    seo = {
        "title": title,
        "description": description,
        "canonical_url": canonical_url,
        "og_title": page_title or default_title,
        "og_description": description,
        "og_image": og_image,
        "og_url": canonical_url,
        "site_name": site_name,
        "index": index,
        "follow": follow,
        "structured_data_items": [
            json.dumps(item, separators=(",", ":")) for item in structured_data
        ],
    }

    ga_id = getattr(settings, "GOOGLE_ANALYTICS_ID", "")

    return {"seo": seo, "ga_id": ga_id}
