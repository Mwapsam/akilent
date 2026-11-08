"""Read-only diagnosis: why does one comment show several times in /inbox/comments/?

Run on prod (piping into `shell` breaks on the loops; use -c):
  python manage.py shell -c "exec(open('docs/audits/2026-11-08-duplicate-comments-diagnosis.py', encoding='utf-8').read())"
It changes nothing.

It answers, for Instagram and Facebook:
  A. Is the same platform comment id stored more than once (in different threads or
     under different Instagram account rows)?           -> ingestion bug; needs the constraint
  B. Every stored copy of the reported text, with ids, thread, account and exact time.
  C. Same text from the same person within 5 minutes, but different comment ids?
                                                         -> really posted several times (test data)
  D. Did the same comment arrive in several webhook events?   -> event dedupe key bug
"""

from datetime import timedelta

from django.db.models import Count, TextField
from django.db.models.functions import Cast

from apps.facebook.models.comment import FacebookComment
from apps.instagram.models.comment import Comment
from apps.instagram.models.webhook import WebhookEventLog

NEEDLE = "How much does it cost to join TaskCentro"

SOURCES = (
    (
        "Instagram",
        Comment,
        "thread__instagram_account_id",
        "thread__instagram_account__account_id",
        "instagram_contact_id",
    ),
    ("Facebook", FacebookComment, "thread__page_id", "thread__account_id", "facebook_contact_id"),
)

for label, model, channel_account, workspace, author in SOURCES:
    print(f"\n===== {label} =====")

    dupes = list(
        model.objects.values("comment_id").annotate(n=Count("id")).filter(n__gt=1)[:25]
    )
    print(f"A. platform ids stored more than once: {len(dupes)} (first 25)")
    for row in dupes:
        copies = model.objects.filter(comment_id=row["comment_id"]).values_list(
            "id", "thread_id", channel_account, workspace, "timestamp"
        )
        print("   ", row["comment_id"], list(copies))

    print(f"B. stored copies of {NEEDLE!r}:")
    hits = list(
        model.objects.filter(body__icontains=NEEDLE)
        .order_by("timestamp")
        .values_list(
            "id", "comment_id", "thread_id", channel_account, workspace, author, "timestamp", "direction"
        )
    )
    for hit in hits:
        print("   ", hit)

    print("C. same text + same author within 5 min, different ids:")
    seen = {}
    for pk, cid, _t, _ca, _ws, who, ts, _d in hits:
        for other_cid, other_ts in seen.get(who, []):
            if other_cid != cid and abs(ts - other_ts) <= timedelta(minutes=5):
                print(f"    author {who}: {other_cid} @ {other_ts:%Y-%m-%d %H:%M:%S} and {cid} @ {ts:%Y-%m-%d %H:%M:%S}")
        seen.setdefault(who, []).append((cid, ts))

print("\n===== D. Instagram webhook events carrying the reported comment ids =====")
ids = set(Comment.objects.filter(body__icontains=NEEDLE).values_list("comment_id", flat=True))
comment_events = WebhookEventLog.objects.filter(
    event_type=WebhookEventLog.EventType.COMMENT
).annotate(payload_text=Cast("raw_payload", TextField()))
for cid in ids:
    events = comment_events.filter(payload_text__contains=cid).values_list(
        "id", "instagram_account_id", "event_id", "status", "received_at"
    )
    print("   ", cid, list(events))

print("\nDone. Nothing was changed.")
