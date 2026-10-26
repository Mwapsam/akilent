"""Idempotency keys for replies sent from the inbox, and who sent them.

The inbox script gives every send attempt a random key and sends it again with the same key
when the person retries a send whose outcome was unknown. The server builds the full key from
it, ``inbox:<conversation>:<user>:<client key>``, so:

* a retry with the same key returns the first send instead of messaging the customer twice
  (WhatsApp dedupes on the key per account, Instagram on the key itself);
* the projection onto the inbox thread can record which teammate sent it (``sent_by_user``)
  and the browser's key (``client_key``), so the script matches its "Sending…" or
  "Not confirmed" bubble to exactly its own message, never a teammate's.

The person comes from the request, never from the browser, and the conversation is part of
the key, so one person's key can't stand for another's send or another conversation's.
"""

import re

PREFIX = "inbox:"
_CLIENT_KEY = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def inbox_key(conversation, user, client_key: str | None) -> str | None:
    """The idempotency key for this send, or None if the browser sent no usable key."""
    if not client_key or not _CLIENT_KEY.match(client_key) or user is None:
        return None
    return f"{PREFIX}{conversation.pk}:{user.pk}:{client_key}"


def is_inbox_key(key: str | None) -> bool:
    return bool(key) and key.startswith(PREFIX)  # type: ignore[union-attr]


def sender_metadata(key: str | None) -> dict:
    """``{"sent_by_user": <user pk>, "client_key": "<key>"}`` for an inbox key, else ``{}``.

    Kept apart from ``sent_by``, whose absence is how the rest of the app recognises a
    person's reply (reports, follow-ups, AI context).
    """
    if not is_inbox_key(key):
        return {}
    parts = key[len(PREFIX) :].split(":", 2)  # type: ignore[index]
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2]:
        return {}
    return {"sent_by_user": int(parts[1]), "client_key": parts[2]}
