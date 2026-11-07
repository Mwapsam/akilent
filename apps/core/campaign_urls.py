"""Campaigns under one address (Campaigns plan, Phase C): /campaigns/.

The list (apps.email.views.campaigns_list, which shows email and WhatsApp campaigns), the
WhatsApp composer and campaign pages (apps.whatsapp.views) and Lists, reached from here so the
sidebar stays on Campaigns. URL names are unchanged, so every ``{% url %}`` and ``reverse()``
lands here. The old /email/campaigns/ and /whatsapp/campaigns/… addresses redirect
(``moved``); the public API keeps its own paths.

The WhatsApp pages answer only while WhatsApp is switched on, as they did under /whatsapp/
(where ChannelGateMiddleware hid them).
"""

from functools import wraps

from django.http import Http404, HttpResponsePermanentRedirect
from django.urls import path

from apps.contacts import views as contact_views
from apps.email import views as email_views
from apps.whatsapp import views as wa_views


def _whatsapp(view):
    @wraps(view)
    def gated(request, *args, **kwargs):
        from apps.core.channels import is_enabled

        if not is_enabled("whatsapp"):
            raise Http404("WhatsApp is not enabled")
        return view(request, *args, **kwargs)

    return gated


def _any_campaign_channel(view):
    """The list shows email and WhatsApp campaigns: there's nothing to show with both off
    (it 404'd that way at /email/campaigns/, through ChannelGateMiddleware's hub rule)."""

    @wraps(view)
    def gated(request, *args, **kwargs):
        from apps.core.channels import is_enabled

        if not (is_enabled("email") or is_enabled("whatsapp")):
            raise Http404("No campaign channel is enabled")
        return view(request, *args, **kwargs)

    return gated


class PermanentRedirectKeepMethod(HttpResponsePermanentRedirect):
    status_code = 308


def moved(request, rest: str = ""):
    """An old campaigns address: to the same page under /campaigns/, query string kept. A GET
    is a 301; anything else a 308, so a form posted to an old address still posts."""
    target = f"/campaigns/{rest}"
    if request.META.get("QUERY_STRING"):
        target += "?" + request.META["QUERY_STRING"]
    if request.method in ("GET", "HEAD"):
        return HttpResponsePermanentRedirect(target)
    return PermanentRedirectKeepMethod(target)


urlpatterns = [
    path("", _any_campaign_channel(email_views.campaigns_list), name="email-campaigns"),
    path("lists/", contact_views.list_index, name="campaign-lists"),
    path("new/", _whatsapp(wa_views.campaign_new), name="whatsapp-campaign-new"),
    path(
        "estimate/",
        _whatsapp(wa_views.campaign_estimate),
        name="whatsapp-campaign-estimate",
    ),
    path("test/", _whatsapp(wa_views.campaign_test), name="whatsapp-campaign-test"),
    path(
        "settings/",
        _whatsapp(wa_views.campaign_settings),
        name="whatsapp-campaign-settings",
    ),
    path(
        "<int:pk>/",
        _whatsapp(wa_views.campaign_detail),
        name="whatsapp-campaign-detail",
    ),
    path(
        "<int:pk>/export.csv",
        _whatsapp(wa_views.campaign_export),
        name="whatsapp-campaign-export",
    ),
    path(
        "<int:pk>/<str:action>/",
        _whatsapp(wa_views.campaign_action),
        name="whatsapp-campaign-action",
    ),
]
