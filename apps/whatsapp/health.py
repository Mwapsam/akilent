"""Per-number health for the numbers page: Connection / Messaging / Webhooks.

Built for templates so they never inspect tokens, PINs or registration state.
Each item is ``{label, state, detail, when}`` with state ``ok`` / ``warn`` /
``none`` (not yet known). Sends are queued per account, not per number, so the
send diagnostics are account-scoped; webhooks are matched by phone_number_id.
"""
from datetime import timedelta

from django.utils import timezone

from apps.whatsapp.models.outbound import OutboundMessage
from apps.whatsapp.models.webhook import WebhookEventLog

OK, WARN, NONE = "ok", "warn", "none"
HELP_URL = "/help/whatsapp-setup/"
DEGRADED_STREAK = 3
DEGRADED_WINDOW = timedelta(hours=24)


def is_degraded(account) -> bool:
    """Repeated terminal send failures, most recent within the window.

    One failure is normal (bad recipient, transient error) and must not flip
    the number; a later successful send ends the streak, restoring READY.
    """
    recent = list(
        OutboundMessage.objects.filter(
            account=account,
            status__in=[OutboundMessage.Status.SENT, OutboundMessage.Status.FAILED],
        )
        .order_by("-updated_at")
        .values_list("status", "updated_at")[:DEGRADED_STREAK]
    )
    return (
        len(recent) == DEGRADED_STREAK
        and all(status == OutboundMessage.Status.FAILED for status, _ in recent)
        and timezone.now() - recent[0][1] <= DEGRADED_WINDOW
    )


def last_webhook_at(number):
    return (
        WebhookEventLog.objects.filter(
            payload__entry__0__changes__0__value__metadata__phone_number_id=number.phone_number_id
        )
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )


def _item(label, state, detail="", when=None):
    return {"label": label, "state": state, "detail": detail, "when": when}


def number_health(number, *, embedded_enabled: bool = False) -> dict:
    S = number.SetupStatus
    R = number.RegistrationStatus
    status = number.setup_status
    has_creds = bool(number.access_token and number.waba_id)

    if not number.access_token:
        creds = _item(
            "Access credentials", WARN,
            "No access token — this number can't send. Reconnect WhatsApp or add a token.",
        )
    else:
        creds = _item("Access credentials", OK)

    if number.registration_status == R.REGISTERED:
        registration = _item("Cloud API registration", OK)
    elif number.registration_status == R.FAILED:
        detail = "Meta couldn't register this number for the Cloud API."
        if number.registration_error:
            detail += f" Meta said: {number.registration_error}"
        registration = _item("Cloud API registration", WARN, detail)
    else:
        registration = _item(
            "Cloud API registration", WARN if has_creds else NONE,
            "This number isn't registered on the Cloud API yet, so it can't send.",
        )

    connection = [
        _item("WhatsApp Business Account", OK if number.waba_id else WARN,
              "" if number.waba_id else "No WhatsApp Business Account ID on this number."),
        creds,
        registration,
    ]

    test = number.last_successful_test()
    last_sent = (
        OutboundMessage.objects.filter(
            account=number.account, status=OutboundMessage.Status.SENT
        ).order_by("-sent_at").values_list("sent_at", flat=True).first()
    )
    last_failed = (
        OutboundMessage.objects.filter(
            account=number.account, status=OutboundMessage.Status.FAILED
        ).order_by("-updated_at").first()
    )
    messaging = [
        _item("Test message", OK, "Sent", test.created_at) if test
        else _item("Test message", NONE, "Not sent yet"),
    ]
    if last_sent:
        messaging.append(_item("Last send", OK, "", last_sent))
    if status == S.DEGRADED and last_failed:
        from apps.whatsapp.friendly_errors import friendly_send_error

        detail = friendly_send_error(last_failed.error_code)
        messaging.append(_item("Recent sends failing", WARN, detail, last_failed.updated_at))

    hook = last_webhook_at(number)
    webhooks = [
        _item("Last webhook received", OK, "", hook) if hook
        else _item("Webhook", NONE, "No webhook received yet"),
    ]

    actions = []
    if status in (S.FAILED,) or (has_creds and number.registration_status != R.REGISTERED):
        actions.append({
            "label": "Retry registration", "method": "post",
            "url": f"/whatsapp/numbers/{number.pk}/register/", "primary": True,
        })
    if not number.access_token and embedded_enabled:
        actions.append({"label": "Reconnect", "method": "get",
                        "url": "/whatsapp/connect/redirect/", "primary": True})
    if status in (S.READY, S.DEGRADED):
        actions.append({"label": "Open Inbox", "method": "get", "url": "/inbox/",
                        "primary": status == S.READY})
    actions.append({"label": "Troubleshoot", "method": "get", "url": HELP_URL, "primary": False})

    headline = {
        S.READY: ("Active", "success"),
        S.TEST_SENT: ("Almost ready", "warning"),
        S.READY_FOR_TEST: ("Ready to test", "warning"),
        S.REGISTERING: ("Registering", "warning"),
    }.get(status, ("Attention needed", "danger"))

    return {
        "status": status,
        "headline": headline[0],
        "tone": headline[1],
        "groups": [
            {"title": "Connection", "items": connection},
            {"title": "Messaging", "items": messaging},
            {"title": "Webhooks", "items": webhooks},
        ],
        "actions": actions,
    }
