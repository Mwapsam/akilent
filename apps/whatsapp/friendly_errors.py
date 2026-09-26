"""Plain-language explanations for WhatsApp send failures shown to business users.

Two different things go wrong at two different times:
- a *test* send during setup (``verification.py``'s own ``_ERRORS``), and
- a *real* send to a customer, failing either as a policy check
  (``apps.whatsapp.tasks.SendNotAuthorized``, whose ``code`` is one of ours below)
  or as a raw Meta Graph API error code (digits, e.g. "131047").

This module is read by the outbound-projection code (to store a reason on the
conversation spine) and by any other surface that shows a WhatsApp send failure
to a non-technical user. It must never be the only place error text lives — the
raw code is always kept alongside it.
"""

# Meta Graph API error codes (digits) seen on a real send, not only the
# setup test-message flow. Keyed as strings, matching how they're stored.
_GRAPH_ERRORS = {
    "131047": "This customer hasn't messaged in the last 24 hours, so WhatsApp only "
               "allows an approved message template, not a plain reply.",
    "131037": "Meta isn't letting this WhatsApp number send yet: its display name needs Meta's "
               "approval. For a number Meta gave you (+1 555…), that usually means your business "
               "verification must finish first. Check Business Settings and WhatsApp Manager in Meta.",
    "131030": "This phone number can't receive messages from this WhatsApp account "
               "yet (it may need to be added as an allowed recipient, or the "
               "account has restrictions).",
    "131026": "WhatsApp couldn't deliver this message. The number may not have "
               "WhatsApp, or has blocked this business.",
    "131053": "The file or image couldn't be sent — WhatsApp rejected the media.",
    "190": "The WhatsApp connection needs to be reconnected.",
}

# Our own policy codes, raised before a send is even attempted (SendNotAuthorized.code).
_POLICY_ERRORS = {
    "OUTSIDE_WINDOW_NO_TEMPLATE": "This customer hasn't messaged in the last 24 hours, so "
                                   "WhatsApp only allows an approved message template, not "
                                   "a plain reply.",
    "TEMPLATE_NOT_APPROVED": "This message template hasn't been approved by Meta yet.",
    "MARKETING_REQUIRES_OPT_IN": "This customer hasn't opted in to marketing messages.",
    "CONTACT_OPTED_OUT": "This customer has opted out of WhatsApp messages (replied STOP).",
}

_DEFAULT = "This message could not be delivered."


def friendly_send_error(error_code: str | None) -> str:
    """One sentence a business owner can read, for a failed outbound message.

    Always falls back to something plain rather than raising or returning None,
    so a code we haven't mapped yet still shows *something* useful.
    """
    code = (error_code or "").strip()
    if not code:
        return _DEFAULT
    # A policy code, or a Graph error code possibly followed by "/<subcode>".
    return _POLICY_ERRORS.get(code) or _GRAPH_ERRORS.get(code.split("/")[0]) or (
        f"{_DEFAULT} (error {code})"
    )
