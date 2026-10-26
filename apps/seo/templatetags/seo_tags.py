from django import template

register = template.Library()


@register.inclusion_tag("seo/meta.html", takes_context=True)
def seo_meta(context, title=True):
    """The page's SEO tags. ``title=False`` for a layout with its own <title> block: two
    <title> tags and the browser shows the first, which was this one, the site's generic
    title, on every app page."""
    return {"seo": context.get("seo", {}), "with_title": title}


@register.inclusion_tag("analytics/google.html", takes_context=True)
def google_analytics(context):
    return {"ga_id": context.get("ga_id", "")}
