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
    """``{"subject", "text_body", "html_body"}`` for this account's report, or None when
    nothing happened this week — an inactive business isn't emailed a blank report.

    Both bodies are built from the same stats and sections, so they can't drift apart.
    """
    from apps.accounts.notifications import absolute_url

    data = proof(account, week=week, now=week.end)
    if not data["active"]:
        return None

    intro = f"This week, Akilent helped your business: {data['sentence']}."

    # (label, value) rows: "label: value" lines in text, the details table in HTML.
    stats = [("Median first reply", duration(data["median"]["seconds"]))]
    rec = data["recovered"]
    if rec["missed"]:
        recovery = (
            f"{rec['rate_pct']}% "
            f"({rec['recovered']} of {rec['missed']} missed conversations recovered)"
        )
        if rec["became_leads"]:
            recovery += f", {rec['became_leads']} became leads"
        stats.append(("Recovery rate", recovery))
    paid = data["paid"]
    money = _money_line(paid["revenue"])
    stats.append(
        (
            "Paid orders from conversations",
            f"{paid['orders']}{f' ({money})' if money else ''}",
        )
    )

    # (title or None, lines): a titled bullet list, or plain lines.
    sections: list[tuple[str | None, list[str]]] = []
    risks = at_risk(account, week.end)["actions"][:TOP_RISKS]
    if risks:
        sections.append(("Opportunities at risk", [action["text"] for action in risks]))
    goal_lines = _goal_lines(account, week)
    if goal_lines:
        sections.append((None, goal_lines))
    if full:
        rows = team(account, week)
        if rows:
            sections.append(
                (
                    "Team",
                    [
                        f"{row['name']}: {row['conversations']} conversations, "
                        f"median reply {duration(row['median_first_reply_seconds'])}"
                        for row in rows
                    ],
                )
            )

    insights_url = absolute_url("/insights/")
    lines = [intro, ""]
    lines.extend(f"{label}: {value}" for label, value in stats)
    for title, items in sections:
        lines.append("")
        if title:
            lines.append(f"{title}:")
            lines.extend(f"- {item}" for item in items)
        else:
            lines.extend(items)
    lines.append("")
    lines.append(f"Open Insights: {insights_url}")

    subject = f"Your week with Akilent: {data['sentence']}"
    text_body = "\n".join(lines)
    try:
        from apps.email.services.html_email import build_transactional_html

        html_sections = [
            (f"{title}:\n" + "\n".join(f"• {item}" for item in items))
            if title
            else "\n".join(items)
            for title, items in sections
        ]
        html_body = build_transactional_html(
            "\n\n".join(html_sections),
            subject[:200],
            eyebrow="Weekly report",
            heading=f"This week: {data['sentence']}",
            preheader=intro,
            details=stats,
            cta_url=insights_url,
            cta_label="Open Insights",
            reason="You're receiving this weekly report as an owner. "
            "You can turn it off on the Insights page.",
        )
    except Exception:
        html_body = ""
    return {"subject": subject[:200], "text_body": text_body, "html_body": html_body}
