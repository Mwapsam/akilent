"""What an unconfirmed owner email holds back: campaign sends, email and WhatsApp alike.

Everything else works straight away (signup is never gated): the inbox, replies, connecting
channels, Embedded Signup. Campaigns go to many people at once, so they wait until the owner
has proved the address we'd hear back on, with the code from signup or the link in the same
email (accounts.views.signup_verify). Accounts that existed before this rule were marked
confirmed by migration accounts/0029, so nobody lost sending when it shipped.
"""

MESSAGE = (
    "Confirm your email address before sending campaigns. Enter the code we emailed you "
    "(Setup → Confirm your email address)."
)


def can_send_campaigns(account) -> bool:
    return bool(account is not None and account.email_verified)
