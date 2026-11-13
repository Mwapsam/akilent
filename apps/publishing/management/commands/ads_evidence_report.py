"""Gate 0 of docs/strategy/akilent-ads.md: is there enough organic evidence to rank posts?

    python manage.py ads_evidence_report [--days 90] [--min-share 20]

Read-only. Prints sanitized JSON (no names, no message text, no ids beyond counts):

- active workspaces, and how many can publish posts at all;
- the count and share of active workspaces with at least one post above each label's
  floor (apps.publishing.promote.FLOORS), over the window;
- the distribution of confirmed organic chats, leads and paid orders per workspace;
- how many Meta ad referrals are stored (apps.conversations.AdReferral), and how many
  matched a Content Studio post;
- whether post age can be computed (published posts with a published_at);
- product availability: never verifiable (no post-to-product link);
- the stop condition: fewer than ``--min-share`` % of active workspaces clearing the
  "Most chats" floor means stop and revisit the differentiator.

The share is of *all* active workspaces, never of a hand-picked few.
"""

from __future__ import annotations

import json
from statistics import median

from django.core.management.base import BaseCommand
from django.utils import timezone


def _dist(values: list[int]) -> dict:
    if not values:
        return {"workspaces": 0, "zero": 0, "median": None, "max": None, "total": 0}
    return {
        "workspaces": len(values),
        "zero": sum(1 for v in values if v == 0),
        "median": median(values),
        "max": max(values),
        "total": sum(values),
    }


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


class Command(BaseCommand):
    help = "Gate 0: share of workspaces with posts above the promotion floors (read-only)"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None)
        parser.add_argument("--min-share", type=float, default=20.0)

    def handle(self, *args, **opts):
        from apps.accounts.models import Account
        from apps.conversations import reporting
        from apps.conversations.models import AdReferral
        from apps.publishing import promote, sales
        from apps.publishing.models import ContentPost

        days = opts["days"] or promote.DAYS
        now = timezone.now()
        period = reporting.last_days(days, now)
        accounts = Account.objects.filter(is_active=True).order_by("pk")

        active = can_publish = 0
        above = {"sold": 0, "leads": 0, "chats": 0}
        per_account = {"chats": [], "leads": [], "paid_orders": []}
        for account in accounts.iterator():
            active += 1
            report = sales.report(account, period, min_chats=0)
            if report is None:
                continue
            can_publish += 1
            rows = report["ranked"]
            for metric in per_account:
                per_account[metric].append(sum(r["confirmed"][metric] for r in rows))
            # Each floor separately (a post above "sold" is not counted as "chats").
            for key, metric in (
                ("sold", "paid_orders"),
                ("leads", "leads"),
                ("chats", "chats"),
            ):
                if any(r["confirmed"][metric] >= promote.FLOORS[key] for r in rows):
                    above[key] += 1

        published = ContentPost.objects.filter(
            account__is_active=True, status=ContentPost.Status.PUBLISHED
        )
        referrals = AdReferral.objects.filter(account__is_active=True)
        chats_share = _pct(above["chats"], active)
        out = {
            "generated_at": now.isoformat(),
            "window_days": days,
            "floors": promote.FLOORS,
            "active_workspaces": active,
            "workspaces_that_can_publish": can_publish,
            "above_floor": {
                key: {"count": n, "share_pct": _pct(n, active)}
                for key, n in above.items()
            },
            "confirmed_organic_per_workspace": {
                k: _dist(v) for k, v in per_account.items()
            },
            "ad_referrals": {
                "stored": referrals.count(),
                "workspaces": referrals.values("account").distinct().count(),
                "matched_to_a_post": referrals.filter(post__isnull=False).count(),
                "by_channel": {
                    c: referrals.filter(channel=c).count()
                    for c in AdReferral.Channel.values
                },
            },
            "post_age": {
                "published_posts": published.count(),
                "with_published_at": published.filter(
                    published_at__isnull=False
                ).count(),
            },
            "product_availability": "not verifiable: posts have no product link",
            "stop_condition": {
                "rule": f"stop if fewer than {opts['min_share']:g}% of active "
                "workspaces clear the 'Most chats' floor",
                "most_chats_share_pct": chats_share,
                "stop": chats_share is None or chats_share < opts["min_share"],
            },
        }
        self.stdout.write(json.dumps(out, indent=2, default=str))
