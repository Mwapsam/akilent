"""Step 0a baseline: how quickly are WhatsApp enquiries answered *today*?

Read-only measurement over the "old world" data (``MessageLog`` plus the
Lead/Deal/Order tables). It deliberately never reads ``conversations.Message``:
Phase 1 changes what that table means, and this baseline must not move with it.

Definitions (see ``baseline_schema.LIMITATIONS`` for what they do not capture):

* **Exchange** – a run of messages for one WhatsApp contact where no two
  consecutive messages (either direction) are ``gap`` or more apart.
* **Enquiry** – an exchange whose first message is an inbound, non-STOP/START
  message and which starts inside the ``[since, until)`` window.
* **Answered** – the exchange holds an outbound with status sent/delivered/read.
  First response = earliest such outbound minus the enquiry's first inbound.
* **Unanswered** – no outbound at all in the exchange and the enquiry is older
  than the grace period. **Never answered** – unanswered *and* the contact has no
  delivered-or-better outbound at any later time.
* **Indeterminate** – cannot be classified reliably; always counted with a
  reason, never forced into a bucket.

Every MessageLog query is filtered by ``account_id``. Cross-account totals are
only ever the arithmetic sum of independently computed per-account results.
"""
import math
import statistics
from collections import Counter, namedtuple
from datetime import timedelta
from itertools import groupby

from django.apps import apps
from django.conf import settings

from .baseline_schema import (
    BUCKET_NAMES,
    BUCKET_UPPER_BOUNDS_SECONDS,
    BUSINESS_HOURS_ADJUSTMENT,
    LIMITATIONS,
    PERCENTILE_MIN_SAMPLES,
    SCHEMA_VERSION,
    SOURCE,
)
from .models import MessageLog

QUALIFYING_STATUSES = (
    MessageLog.Status.SENT,
    MessageLog.Status.DELIVERED,
    MessageLog.Status.READ,
)
_CHUNK = 500

Row = namedtuple("Row", "ts direction status message_type created_at keyword wa_contact_fk")
Enquiry = namedtuple("Enquiry", "start contact_fk outcome reason response_seconds never_answered")


def _is_consent_keyword(message_type, content) -> bool:
    if message_type != MessageLog.MessageType.TEXT:
        return False
    token = (content or "").strip().strip(".!?").upper()
    return bool(token) and (
        token in settings.WHATSAPP_STOP_KEYWORDS or token in settings.WHATSAPP_START_KEYWORDS
    )


def _load_rows(account, lower_bound):
    """Every MessageLog row for ONE account, grouped per WhatsApp contact, oldest first."""
    qs = MessageLog.objects.filter(account_id=account.id)
    if lower_bound is not None:
        qs = qs.filter(timestamp__gte=lower_bound)
    qs = qs.order_by("contact_id", "timestamp", "id").values_list(
        "contact_id", "direction", "timestamp", "status", "message_type",
        "created_at", "content", "contact__contact_id",
    )
    for contact_id, group in groupby(qs.iterator(), key=lambda r: r[0]):
        yield contact_id, [
            Row(ts, direction, status, mtype, created_at,
                _is_consent_keyword(mtype, content), contact_fk)
            for _cid, direction, ts, status, mtype, created_at, content, contact_fk in group
        ]


def _exchanges(rows, gap):
    current, prev_ts = [], None
    for row in rows:
        if current and row.ts - prev_ts >= gap:
            yield current
            current = []
        current.append(row)
        prev_ts = row.ts
    if current:
        yield current


def _classify(exchange, contact_rows, as_of, grace, account_has_outbound):
    """Return (outcome, reason, response_seconds, never_answered) for one enquiry."""
    first = exchange[0]
    if not account_has_outbound:
        return "indeterminate", "no_outbound_logged_for_account", None, False

    qualifying = [r for r in exchange
                  if r.direction == MessageLog.Direction.OUTBOUND and r.status in QUALIFYING_STATUSES]
    if qualifying:
        reply = qualifying[0]  # rows are time-ordered
        if reply.created_at < first.created_at:
            # Logged before the enquiry was even received: a pre-queued proactive
            # send, not a reply. The clocks cannot be trusted for a response time.
            return "indeterminate", "outbound_created_before_enquiry", None, False
        return "answered", None, (reply.ts - first.ts).total_seconds(), False

    if any(r.direction == MessageLog.Direction.OUTBOUND for r in exchange):
        return "indeterminate", "outbound_not_delivered", None, False  # queued/failed only
    if as_of - first.ts < grace:
        return "indeterminate", "within_grace_period", None, False

    never = not any(
        r.direction == MessageLog.Direction.OUTBOUND and r.status in QUALIFYING_STATUSES
        and r.ts >= first.ts
        for r in contact_rows
    )
    return "unanswered", None, None, never


def _percentile(sorted_values, q):
    return sorted_values[max(math.ceil(q * len(sorted_values)) - 1, 0)]


def _bucket(seconds):
    for name, upper in zip(BUCKET_NAMES, BUCKET_UPPER_BOUNDS_SECONDS):
        if seconds < upper:
            return name
    return BUCKET_NAMES[-1]


def _in_window(ts, since, until):
    return (since is None or ts >= since) and (until is None or ts < until)


def _cohort_counts(account, enquiries, until):
    """Enquiries whose contact later acquired a lead/deal/order (descriptive only)."""
    linked = {e.contact_fk for e in enquiries if e.contact_fk}
    counts = {}
    for key, (app_label, model_name) in {
        "enquiries_with_lead": ("crm", "Lead"),
        "enquiries_with_deal": ("crm", "Deal"),
        "enquiries_with_order": ("commerce", "Order"),
    }.items():
        model = apps.get_model(app_label, model_name)
        created = {}
        ids = sorted(linked)
        for i in range(0, len(ids), _CHUNK):
            for contact_id, created_at in model.objects.filter(
                account_id=account.id, contact_id__in=ids[i:i + _CHUNK]
            ).values_list("contact_id", "created_at"):
                created.setdefault(contact_id, []).append(created_at)
        counts[key] = sum(
            1 for e in enquiries
            if e.contact_fk and any(
                c >= e.start and (until is None or c < until)
                for c in created.get(e.contact_fk, ())
            )
        )
    return counts


def _result(gap_hours, enquiries, cohort, data_quality):
    outcomes = Counter(e.outcome for e in enquiries)
    reasons = Counter(e.reason for e in enquiries if e.outcome == "indeterminate")
    answered = sorted(e.response_seconds for e in enquiries if e.outcome == "answered")
    buckets = Counter(_bucket(s) for s in answered)
    enough = len(answered) >= PERCENTILE_MIN_SAMPLES
    return {
        "enquiry_gap_hours": gap_hours,
        "enquiries": {
            "inbound": len(enquiries),
            "answered": outcomes["answered"],
            "unanswered": outcomes["unanswered"],
            "never_answered": sum(1 for e in enquiries if e.never_answered),
            "indeterminate": outcomes["indeterminate"],
            "indeterminate_reasons": dict(sorted(reasons.items())),
        },
        "response_time": {
            **{name: buckets[name] for name in BUCKET_NAMES},
            "median_seconds": round(statistics.median(answered), 1) if enough else None,
            "p90_seconds": round(_percentile(answered, 0.9), 1) if enough else None,
        },
        "conversion_cohort": cohort,
        "data_quality": data_quality,
    }


def measure_account(account, gap_hours_list, grace_hours, since, until, as_of):
    """Independent measurement of ONE account: one result per enquiry gap."""
    gaps = [timedelta(hours=h) for h in gap_hours_list]
    lower_bound = since - max(gaps) if since is not None else None
    grace = timedelta(hours=grace_hours)

    by_contact = list(_load_rows(account, lower_bound))
    window_rows = [r for _cid, rows in by_contact for r in rows if _in_window(r.ts, since, until)]
    quality = {
        "inbound_rows": sum(r.direction == MessageLog.Direction.INBOUND for r in window_rows),
        "outbound_rows": sum(r.direction == MessageLog.Direction.OUTBOUND for r in window_rows),
        "outbound_failed": sum(
            r.direction == MessageLog.Direction.OUTBOUND and r.status == MessageLog.Status.FAILED
            for r in window_rows),
        "outbound_template": sum(
            r.direction == MessageLog.Direction.OUTBOUND
            and r.message_type == MessageLog.MessageType.TEMPLATE for r in window_rows),
    }
    account_has_outbound = quality["outbound_rows"] > 0

    results = []
    for gap_hours, gap in zip(gap_hours_list, gaps):
        enquiries = []
        for _cid, rows in by_contact:
            for exchange in _exchanges(rows, gap):
                first = exchange[0]
                if (first.direction != MessageLog.Direction.INBOUND or first.keyword
                        or not _in_window(first.ts, since, until)):
                    continue
                outcome, reason, seconds, never = _classify(
                    exchange, rows, as_of, grace, account_has_outbound)
                enquiries.append(Enquiry(first.ts, first.wa_contact_fk, outcome, reason, seconds, never))
        results.append(_result(gap_hours, enquiries, _cohort_counts(account, enquiries, until), dict(quality)))
    return results


def sum_results(per_account_results):
    """Arithmetic total of independently computed per-account results (never an unscoped query)."""
    by_gap = {}
    for results in per_account_results:
        for r in results:
            by_gap.setdefault(r["enquiry_gap_hours"], []).append(r)
    totals = []
    for gap_hours, items in sorted(by_gap.items()):
        reasons = Counter()
        for r in items:
            reasons.update(r["enquiries"]["indeterminate_reasons"])
        totals.append({
            "enquiry_gap_hours": gap_hours,
            "enquiries": {
                **{k: sum(r["enquiries"][k] for r in items)
                   for k in ("inbound", "answered", "unanswered", "never_answered", "indeterminate")},
                "indeterminate_reasons": dict(sorted(reasons.items())),
            },
            "response_time": {
                **{name: sum(r["response_time"][name] for r in items) for name in BUCKET_NAMES},
                "median_seconds": None,  # not derivable from per-account aggregates
                "p90_seconds": None,
            },
            "conversion_cohort": {
                k: sum(r["conversion_cohort"][k] for r in items)
                for k in ("enquiries_with_lead", "enquiries_with_deal", "enquiries_with_order")
            },
            "data_quality": {
                k: sum(r["data_quality"][k] for r in items)
                for k in ("inbound_rows", "outbound_rows", "outbound_failed", "outbound_template")
            },
        })
    return totals


def build_snapshot(accounts, gap_hours_list, grace_hours, since, until, since_arg, until_arg,
                   as_of, git_commit, include_total=False):
    """Measure each account independently and assemble the v1.0 snapshot."""
    entries = [
        {
            "account": {"id": account.id, "slug": account.slug},
            "results": measure_account(account, gap_hours_list, grace_hours, since, until, as_of),
        }
        for account in accounts
    ]
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "generated_at": as_of.isoformat(),
            "git_commit": git_commit,
            "since": since_arg,
            "until": until_arg,
            "grace_hours": grace_hours,
            "timezone": settings.TIME_ZONE,
            "business_hours_adjustment": BUSINESS_HOURS_ADJUSTMENT,
            "source": SOURCE,
        },
        "accounts": entries,
        "limitations": list(LIMITATIONS),
    }
    if include_total:
        snapshot["total"] = {"results": sum_results([e["results"] for e in entries])}
    return snapshot


def _fmt_seconds(value):
    return "n/a (n<%d)" % PERCENTILE_MIN_SAMPLES if value is None else f"{value:,.0f}s"


def _render_result(r):
    e, rt, c, d = r["enquiries"], r["response_time"], r["conversion_cohort"], r["data_quality"]
    ok = e["answered"] + e["unanswered"] + e["indeterminate"] == e["inbound"]
    lines = [
        f"  Enquiry gap {r['enquiry_gap_hours']}h",
        f"    enquiries: {e['inbound']}  answered {e['answered']}  unanswered {e['unanswered']}"
        f" (never answered {e['never_answered']})  indeterminate {e['indeterminate']}",
    ]
    for reason, n in e["indeterminate_reasons"].items():
        lines.append(f"      indeterminate - {reason}: {n}")
    lines += [
        f"    first response: <5m {rt['lt_5m']} | 5m-1h {rt['5m_to_1h']} | 1h-24h {rt['1h_to_24h']}"
        f" | >=24h {rt['gte_24h']}   (intervals: 0<=r<5m, 5m<=r<1h, 1h<=r<24h, r>=24h)",
        f"    median {_fmt_seconds(rt['median_seconds'])}   p90 {_fmt_seconds(rt['p90_seconds'])}",
        f"    conversion cohort (descriptive, NOT attribution): later lead {c['enquiries_with_lead']}"
        f" | deal {c['enquiries_with_deal']} | order {c['enquiries_with_order']}",
        f"    data quality: inbound rows {d['inbound_rows']}, outbound rows {d['outbound_rows']},"
        f" outbound failed {d['outbound_failed']}, outbound template {d['outbound_template']}",
        f"    reconciliation: answered+unanswered+indeterminate == enquiries: {'OK' if ok else 'FAILED'}",
    ]
    return lines


def render_report(snapshot) -> str:
    m = snapshot["metadata"]
    lines = [
        f"Conversation baseline (schema {snapshot['schema_version']})",
        f"generated {m['generated_at']}  commit {m['git_commit']}  window {m['since'] or '-'} .. {m['until'] or '-'}"
        f"  grace {m['grace_hours']}h  timezone {m['timezone']}  business-hours adjustment: {m['business_hours_adjustment']}",
        "",
    ]
    sections = [(f"Account {e['account']['slug']} (id {e['account']['id']})", e["results"])
                for e in snapshot["accounts"]]
    if "total" in snapshot:
        sections.append(("TOTAL (arithmetic sum of the accounts above)", snapshot["total"]["results"]))
    for title, results in sections:
        lines.append(title)
        for r in results:
            lines += _render_result(r)
        if len(results) > 1:
            lines.append("  Sensitivity (enquiries / answered / unanswered / indeterminate):")
            for r in results:
                e = r["enquiries"]
                lines.append(f"    gap {r['enquiry_gap_hours']:>3}h: {e['inbound']} / {e['answered']}"
                             f" / {e['unanswered']} / {e['indeterminate']}")
        lines.append("")
    lines.append("Limitations")
    lines += [f"  - {text}" for text in snapshot["limitations"]]
    return "\n".join(lines)
