"""Who else is in a conversation right now: teammates viewing it, and teammates typing.

Short-lived cache keys (the Redis cache in production), never database rows: presence is
"in the last few seconds", and losing it on a restart costs nothing. Written by the
conversation's message poll (viewing) and by the composer's typing ping (typing); read
back with one ``get_many`` over the account's team, so nothing ever scans keys.
"""

from __future__ import annotations

from django.core.cache import cache

# The pane polls messages_feed every 3s (backing off when idle or hidden), so a viewer
# lapses a few polls after they leave. The composer pings every 4s while someone types.
VIEWING_TTL = 12
TYPING_TTL = 6


def _key(kind: str, conversation_id: int, user_id: int) -> str:
    return f"presence:{kind}:{conversation_id}:{user_id}"


def mark_viewing(conversation_id: int, user_id: int) -> None:
    cache.set(_key("view", conversation_id, user_id), 1, VIEWING_TTL)


def mark_typing(conversation_id: int, user_id: int) -> None:
    cache.set_many(
        {
            _key("type", conversation_id, user_id): 1,
            _key("view", conversation_id, user_id): 1,
        },
        TYPING_TTL,
    )


def _name(user) -> str:
    return user.get_short_name() or user.get_full_name() or user.get_username()


def for_conversations(conversation_ids, members, exclude_user_id=None) -> dict:
    """``{conversation_id: {"viewing": [names], "typing": [names]}}`` for each conversation
    someone other than ``exclude_user_id`` is in. Someone typing isn't also listed as viewing.
    """
    members = [m for m in members if m.pk != exclude_user_id]
    if not members or not conversation_ids:
        return {}
    keys = [
        _key(kind, cid, m.pk)
        for cid in conversation_ids
        for m in members
        for kind in ("view", "type")
    ]
    found = cache.get_many(keys)
    if not found:
        return {}
    out = {}
    for cid in conversation_ids:
        typing = [m for m in members if _key("type", cid, m.pk) in found]
        viewing = [
            m for m in members if _key("view", cid, m.pk) in found and m not in typing
        ]
        if typing or viewing:
            out[cid] = {
                "typing": [_name(m) for m in typing],
                "viewing": [_name(m) for m in viewing],
            }
    return out


def for_conversation(conversation_id: int, members, exclude_user_id=None) -> dict:
    return for_conversations([conversation_id], members, exclude_user_id).get(
        conversation_id, {"typing": [], "viewing": []}
    )
