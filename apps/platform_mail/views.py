"""Public endpoints for Akilent's marketing email (the unsubscribe link)."""

from django.db import transaction
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from apps.platform_mail.models import MarketingSubscriber
from apps.platform_mail.services import consent


@csrf_exempt  # RFC 8058 one-click POSTs come from mail providers, not our pages
def unsubscribe(request, token: str):
    """GET shows a confirm button; only a POST unsubscribes.

    Mail-security scanners (Safe Links, Mimecast, Proofpoint) open every link in a message
    with a GET, so acting on GET would quietly unsubscribe whole company domains. The POST
    is either our confirm button or the RFC 8058 one-click request the inbox sends.

    Only marketing stops; account emails (password resets, billing) keep coming.
    """
    row = MarketingSubscriber.objects.filter(token=token).first()
    if row is None:
        state = "invalid"
    elif request.method == "POST":
        with transaction.atomic():
            row = MarketingSubscriber.objects.select_for_update().get(pk=row.pk)
            state = "done" if consent.unsubscribe(row, how="email_link") else "already"
    else:
        state = "confirm" if row.is_subscribed else "already"
    return render(
        request,
        "platform_mail/unsubscribe.html",
        {"state": state},
        status=404 if state == "invalid" else 200,
    )
