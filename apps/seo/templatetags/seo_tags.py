from django import template

register = template.Library()


@register.inclusion_tag("seo/meta.html", takes_context=True)
def seo_meta(context):
    return {"seo": context.get("seo", {})}


@register.inclusion_tag("analytics/google.html", takes_context=True)
def google_analytics(context):
    return {"ga_id": context.get("ga_id", "")}
