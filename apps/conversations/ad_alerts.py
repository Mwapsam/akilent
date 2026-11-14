"""Tell the team when chats from Meta ads sit unanswered (release flag ``ads_automation``).

docs/strategy/akilent-ads.md: a paid chat that waits is money the business already spent.
The verdict (``ad_verdict``) reports the leak afterwards; this sweep catches it while the
customer is still there.

- **Waiting** means the same thing as in the verdict: no qualifying reply (holding replies
  and Akilent's own system messages don't count) after ``ad_verdict.UNANSWERED_SECONDS`` of
  business-hours time, counted from the first message of the ad chat.
- Only ad chats started in the last ``WINDOW`` and still open are looked at.
- **One email per workspace per sweep**, to its owners and admins, listing up to
  ``DIGEST_LIMIT`` chats: an ad burst must never become an email burst.
- **Once per ad chat**: each chat is recorded as an ``ad_chat.waiting`` Event keyed on the
  chat and the ad message, so the next sweep doesn't tell anyone again.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

FLAG = "ads_automation"
WINDOW = timedelta(hours=24)
DIGEST_LIMIT = 10
SCAN_LIMIT = 5000  # ad referrals per sweep


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def waiting(now=None, *, account=None, limit: int = SCAN_LIMIT) -> dict:
    """Open ad chats past the threshold with no qualifying reply, per workspace.

    ``{account_id: [(conversation, referral, start), ...]}``, oldest first."""
    from apps.accounts.business_hours import get_hours
    from apps.conversations import ad_verdict
    from apps.conversations.models import AdReferral, Conversation

    now = now or timezone.now()
    refs = AdReferral.objects.filter(
        received_at__gte=now - WINDOW,
        conversation__status=Conversation.Status.OPEN,
    )
    if account is not None:
        refs = refs.filter(account=account)
    refs = refs.select_related(
        "message", "conversation__contact", "conversation__account"
    ).order_by("received_at", "pk")[:limit]

    hours_by_account: dict[int, object] = {}
    seen: set[int] = set()
    out: dict[int, list] = {}
    for ref in refs:
        if ref.conversation_id in seen:
            continue
        seen.add(ref.conversation_id)
        conversation = ref.conversation
        if conversation.account_id not in hours_by_account:
            hours_by_account[conversation.account_id] = get_hours(conversation.account)
        start = ref.message.timestamp if ref.message_id else ref.received_at
        reply = ad_verdict.conversation_reply(
            conversation.pk, start, hours_by_account[conversation.account_id], now
        )
        if reply["replied"] or not reply["late"]:
            continue
        out.setdefault(conversation.account_id, []).append((conversation, ref, start))
    return out


def sweep(now=None) -> dict:
    """Alert each workspace about its newly waiting ad chats. ``{"chats", "emails"}``."""
    if not is_released():
        return {"chats": 0, "emails": 0, "skipped": True}
    from apps.conversations.services import emit_event

    now = now or timezone.now()
    chats = emails = 0
    for items in waiting(now).values():
        fresh = []
        for conversation, ref, start in items:
            event = emit_event(
                account=conversation.account,
                type="ad_chat.waiting",
                occurred_at=now,
                source="ad_alerts",
                source_event_id=f"ad_waiting:{conversation.pk}:{start.isoformat()}",
                payload={
                    "conversation_id": conversation.public_id,
                    "ad_id": ref.source_id,
                },
                subject_type="conversation",
                subject_id=str(conversation.pk),
            )
            if event is not None:
                fresh.append((conversation, ref))
        if not fresh:
            continue
        chats += len(fresh)
        emails += _email(fresh[0][0].account, fresh, now)
    if chats:
        logger.warning("ad_alerts.sweep: chats=%s emails=%s", chats, emails)
    return {"chats": chats, "emails": emails}


def _email(account, fresh: list, now) -> int:
    """One digest to the owners and admins. Best-effort: a mail failure never stops the sweep."""
    from apps.accounts.notifications import notify_team
    from apps.conversations.ad_verdict import UNANSWERED_SECONDS

    n = len(fresh)
    limit = f"{UNANSWERED_SECONDS // 3600} business hour" + (
        "" if UNANSWERED_SECONDS // 3600 == 1 else "s"
    )
    lines = []
    for conversation, ref in fresh[:DIGEST_LIMIT]:
        contact = conversation.contact
        who = (contact.full_name if contact else "") or str(contact or "A customer")
        ad = ref.headline or "your ad"
        lines.append(
            f"- {who} on {conversation.get_channel_display()}, from “{ad[:60]}”"
        )
    if n > DIGEST_LIMIT:
        lines.append(f"- and {n - DIGEST_LIMIT} more")
    chats = "chat" if n == 1 else "chats"
    try:
        return notify_team(
            account,
            to="owners",
            subject=f"{n} {chats} from your Meta ads {'has' if n == 1 else 'have'} "
            f"waited over {limit}",
            text=(
                f"{'This customer' if n == 1 else 'These customers'} tapped your ad and "
                f"messaged you, and nobody has answered for over {limit}:\n\n"
                + "\n".join(lines)
            ),
            path="/inbox/?view=needs_attention&from=ad",
            reason=(
                "You're getting this because Akilent tells your owners and admins when "
                "chats from your Meta ads wait too long for a reply."
            ),
        )
    except Exception:
        logger.exception("ad_alerts: email failed for account=%s", account.pk)
        return 0
