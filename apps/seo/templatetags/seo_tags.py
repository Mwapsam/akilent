from django import template

register = template.Library()


@register.inclusion_tag("seo/meta.html", takes_context=True)
def seo_meta(context, title=True):
    """The page's SEO tags. ``title=False`` for a layout with its own <title> block: two
    <title> tags and the browser shows the first, which was this one, the site's generic
    title, on every app page."""
    return {"seo": context.get("seo", {}), "with_title": title}


@register.inclusion_tag("analytics/google.html", takes_context=True)
def google_analytics(context, events=False):
    """The GA4 loader. ``events=True`` (base.html only) also sends any event a view queued
    for this page (seo.analytics), and drops it even without a GA ID so it can't wait
    around for a later page. The landing and public pages leave it off: reading the
    session there would add Vary: Cookie and stop the edge caching them."""
    from apps.seo.analytics import pop_ga_event

    request = context.get("request")
    event = pop_ga_event(request) if events and request is not None else None
    return {"ga_id": context.get("ga_id", ""), "ga_event": event}
