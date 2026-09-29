"""The weekly owner report: the proof bar and the week's numbers, emailed once a week.

This is Business Health's main retention mechanic — see ``reporting`` for the numbers and the
wording rule they follow (counts, never causes). Content mirrors the page's own order: the proof
bar sentence, the three tiles, the recovery rate, the top Opportunities at risk, then goal status.
A plan with ``detailed_analytics`` additionally gets the team table.
"""

from __future__ import annotations

from apps.conversations.reporting import (
    Period,
    at_risk,
    duration,
    goal_progress,
    proof,
    team,
    week_of,
)
from apps.conversations.snapshots import SETTLE

TOP_RISKS = 3


def report_week(now) -> Period | None:
    """The most recently closed, settled week, or None if this week hasn't settled yet — the
    same settle rule as ``WeeklySnapshot``, so the report never reports on a week still in
    flight."""
    week = week_of(now).previous()
    if now < week.end + SETTLE:
        return None
    return week


def _money_line(rows: list[dict]) -> str:
    paid = [r for r in rows if r["orders"]]
    if not paid:
        return ""
    return ", ".join(f"{r['currency']} {r['total']:,.2f}" for r in paid)


def _goal_lines(account, week: Period) -> list[str]:
    goals = goal_progress(account, now=week.end)
    if not goals["total"]:
        return []
    lines = [f"Goals: {goals['on_track']} of {goals['total']} on track"]
    for row in goals["goals"]:
        if row["on_track"] is None:
            status = "no data yet"
        elif row["on_track"]:
            status = "on track"
        else:
            status = "behind"
        lines.append(f"- {row['label']}: {status}")
    return lines


def build_email(account, week: Period, *, full: bool) -> dict | None:
    """``{"subject", "text_body"}`` for this account's report, or None when nothing happened
    this week — an inactive business isn't emailed a blank report."""
    from apps.accounts.notifications import absolute_url

    data = proof(account, week=week, now=week.end)
    if not data["active"]:
        return None

    lines = [f"This week, Akilent helped your business: {data['sentence']}."]

    lines.append("")
    lines.append(f"Median first reply: {duration(data['median']['seconds'])}")
    rec = data["recovered"]
    if rec["missed"]:
        recovery_line = (
            f"Recovery rate: {rec['rate_pct']}% "
            f"({rec['recovered']} of {rec['missed']} missed conversations recovered)"
        )
        if rec["became_leads"]:
            recovery_line += f", {rec['became_leads']} became leads"
        lines.append(recovery_line)
    paid = data["paid"]
    money = _money_line(paid["revenue"])
    paid_line = f"Paid orders from conversations: {paid['orders']}"
    if money:
        paid_line += f" ({money})"
    lines.append(paid_line)

    risks = at_risk(account, week.end)["actions"][:TOP_RISKS]
    if risks:
        lines.append("")
        lines.append("Opportunities at risk:")
        lines.extend(f"- {action['text']}" for action in risks)

    goal_lines = _goal_lines(account, week)
    if goal_lines:
        lines.append("")
        lines.extend(goal_lines)

    if full:
        rows = team(account, week)
        if rows:
            lines.append("")
            lines.append("Team:")
            for row in rows:
                lines.append(
                    f"- {row['name']}: {row['conversations']} conversations, "
                    f"median reply {duration(row['median_first_reply_seconds'])}"
                )

    lines.append("")
    lines.append(f"Open Insights: {absolute_url('/insights/')}")

    subject = f"Your week with Akilent: {data['sentence']}"
    return {"subject": subject[:200], "text_body": "\n".join(lines)}
