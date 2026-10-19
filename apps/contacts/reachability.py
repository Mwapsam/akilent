"""Can WhatsApp reach this customer — and with what?

One vocabulary everywhere a customer is shown (Customers page, list members,
profile, campaign composer), so the owner sees the same answer the campaign
will act on:

- ``opted_in``   — agreed to hear from the business: any template, marketing included.
- ``updates``    — has a WhatsApp number but hasn't opted in: utility templates
                   (order updates, reminders) only. Marketing skips them.
- ``opted_out``  — sent STOP (or was marked opted out): nothing is sent.
- ``none``       — no phone number: WhatsApp can't reach them.

A customer with a phone but no WhatsApp identity yet counts as ``updates``: a
campaign creates the identity when it sends (apps.whatsapp.campaigns).
"""

from __future__ import annotations

from django.db.models import Case, CharField, Count, OuterRef, Q, Subquery, Value, When

OPTED_IN = "opted_in"
UPDATES = "updates"
OPTED_OUT = "opted_out"
NONE = "none"

STATES = (OPTED_IN, UPDATES, OPTED_OUT, NONE)

LABELS = {
    OPTED_IN: "Opted in",
    UPDATES: "Updates only",
    OPTED_OUT: "Opted out",
    NONE: "No WhatsApp",
}

EXPLANATIONS = {
    OPTED_IN: "Agreed to hear from you — can receive any campaign, including offers.",
    UPDATES: "Can receive updates like order or appointment messages, but not offers "
    "until they opt in.",
    OPTED_OUT: "Asked not to be messaged (replied STOP). Campaigns skip them.",
    NONE: "No phone number, so WhatsApp can't reach them.",
}


def annotate(qs):
    """Add ``wa_state`` (one of STATES) to a Contact queryset."""
    from apps.whatsapp.models import WhatsAppContact

    _Opt = WhatsAppContact.OptInStatus
    identity_status = Subquery(
        WhatsAppContact.objects.filter(contact=OuterRef("pk"))
        # A customer rarely has more than one identity (their number changed).
        # When they do, the most cautious answer wins: opted out, then opted in.
        .annotate(
            rank=Case(
                When(opt_in_status=_Opt.OPTED_OUT, then=Value(0)),
                When(opt_in_status=_Opt.OPTED_IN, then=Value(1)),
                default=Value(2),
            )
        )
        .order_by("rank", "pk")
        .values("opt_in_status")[:1]
    )
    return qs.annotate(_wa_status=identity_status).annotate(
        wa_state=Case(
            When(_wa_status=WhatsAppContact.OptInStatus.OPTED_IN, then=Value(OPTED_IN)),
            When(
                _wa_status=WhatsAppContact.OptInStatus.OPTED_OUT, then=Value(OPTED_OUT)
            ),
            When(_wa_status__isnull=False, then=Value(UPDATES)),
            When(Q(phone__isnull=False) & ~Q(phone=""), then=Value(UPDATES)),
            default=Value(NONE),
            output_field=CharField(),
        )
    )


def filter_state(qs, state: str):
    """Narrow an annotated queryset to one state (unknown values are ignored)."""
    if state in STATES:
        return qs.filter(wa_state=state)
    return qs


def counts(qs) -> dict:
    """``{state: n, ..., "total": n}`` for a Contact queryset, in one query."""
    annotated = annotate(qs.order_by())
    agg = annotated.aggregate(
        total=Count("pk"),
        **{s: Count("pk", filter=Q(wa_state=s)) for s in STATES},
    )
    return {k: v or 0 for k, v in agg.items()}


def state_for(contact) -> str:
    """The state for one contact (profile page)."""
    from apps.contacts.models import Contact

    return (
        annotate(Contact.objects.filter(pk=contact.pk))
        .values_list("wa_state", flat=True)
        .first()
        or NONE
    )


def primary_identity(contact):
    """The WhatsAppContact a campaign would send to for this customer, or None."""
    from apps.whatsapp.models import WhatsAppContact

    identities = list(WhatsAppContact.objects.filter(contact=contact).order_by("pk"))
    for wc in identities:
        if wc.phone_number == contact.phone:
            return wc
    return identities[0] if identities else None
