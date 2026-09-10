"""Deliverability Score — a 0-100 composite of authentication, list hygiene, and
sending reputation, with actionable recommendations.

Consumed by the dashboard card (``templates/email/insights.html``), the public
``GET /v1/deliverability`` endpoint, and the daily ``DeliverabilitySnapshot``
task that powers trend lines and week-over-week deltas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

from apps.email.models import DeliverabilitySnapshot, EmailDomain, SuppressionListEntry
from apps.logs.rates import rates_for

# check status -> factor applied to the check's weight
_FACTOR = {"ok": 1.0, "warn": 0.5, "fail": 0.0}

_MIN_VOLUME_FOR_RATES = 50  # below this, rate-based checks report "na"


@dataclass
class Check:
    key: str
    label: str
    status: str  # ok | warn | fail | na
    detail: str
    weight: int

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "weight": self.weight,
        }


@dataclass
class Score:
    score: int
    grade: str
    checks: list[Check] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "grade": self.grade,
            "checks": [c.as_dict() for c in self.checks],
            "recommendations": self.recommendations,
        }


def _grade(score: int) -> str:
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 60:
        return "Fair"
    return "Needs work"


def _dmarc_policy(dmarc_value: str) -> str | None:
    m = re.search(r"\bp\s*=\s*(none|quarantine|reject)\b", dmarc_value or "", re.I)
    return m.group(1).lower() if m else None


def _auth_checks(domains: list[EmailDomain]) -> list[Check]:
    """Auth is scored on the *weakest* verified domain (a bad one drags delivery)."""
    if not domains:
        return [
            Check("domain_verified", "Verified sending domain", "fail",
                  "No verified sending domain. Add and verify one to start sending.", 15),
            Check("spf", "SPF", "na", "No domain to check.", 10),
            Check("dkim", "DKIM", "na", "No domain to check.", 15),
            Check("dmarc", "DMARC", "na", "No domain to check.", 10),
            Check("dmarc_policy", "DMARC policy strength", "na", "No domain to check.", 5),
        ]

    spf_ok = all(d.spf_ok for d in domains)
    dkim_ok = all(d.dkim_ok for d in domains)
    dmarc_ok = all(d.dmarc_ok for d in domains)
    policies = [_dmarc_policy(d.dmarc_value) for d in domains if d.dmarc_ok]
    weakest_policy = None
    for p in policies:
        rank = {"none": 0, "quarantine": 1, "reject": 2}.get(p or "", -1)
        if weakest_policy is None or rank < {"none": 0, "quarantine": 1, "reject": 2}.get(weakest_policy, 99):
            weakest_policy = p

    if weakest_policy in ("quarantine", "reject"):
        policy_status, policy_detail = "ok", f"Enforcing (p={weakest_policy})."
    elif weakest_policy == "none":
        policy_status, policy_detail = "warn", "p=none only monitors. Move to p=quarantine once your reports look clean."
    else:
        policy_status, policy_detail = "na", "DMARC not verified yet."

    return [
        Check("domain_verified", "Verified sending domain", "ok",
              f"{len(domains)} verified domain(s).", 15),
        Check("spf", "SPF", "ok" if spf_ok else "fail",
              "SPF passes on all domains." if spf_ok else "SPF is missing or failing on at least one domain.", 10),
        Check("dkim", "DKIM", "ok" if dkim_ok else "fail",
              "DKIM passes on all domains." if dkim_ok else "DKIM is missing or failing on at least one domain.", 15),
        Check("dmarc", "DMARC", "ok" if dmarc_ok else "warn",
              "DMARC record found on all domains." if dmarc_ok else "Add a DMARC record — Gmail/Yahoo require it for bulk senders.", 10),
        Check("dmarc_policy", "DMARC policy strength", policy_status, policy_detail, 5),
    ]


def _rate_checks(account, domain) -> list[Check]:
    r = rates_for(account, domain=domain, since_days=30)
    vol = r["volume"]

    if vol < _MIN_VOLUME_FOR_RATES:
        na = f"Only {vol} sends in the last 30 days — not enough to score."
        return [
            Check("bounce_rate", "Bounce rate", "na", na, 15),
            Check("complaint_rate", "Complaint rate", "na", na, 15),
            Check("engagement", "Open engagement", "na", na, 5),
        ]

    br, cr, opr = r["bounce_rate"], r["complaint_rate"], r["open_rate"]

    if br < 0.02:
        b = ("ok", f"{br:.1%} — healthy (target < 2%).")
    elif br < 0.05:
        b = ("warn", f"{br:.1%} — above the 2% comfort zone; clean your list.")
    else:
        b = ("fail", f"{br:.1%} — over 5%. SES may pause sending. Remove invalid addresses now.")

    if cr < 0.001:
        c = ("ok", f"{cr:.2%} — healthy (target < 0.1%).")
    elif cr < 0.003:
        c = ("warn", f"{cr:.2%} — approaching the 0.3% danger line.")
    else:
        c = ("fail", f"{cr:.2%} — over 0.3%. Review consent and send only to engaged recipients.")

    if opr >= 0.15:
        e = ("ok", f"{opr:.0%} unique open rate.")
    elif opr >= 0.05:
        e = ("warn", f"{opr:.0%} unique open rate — low; segment out cold contacts.")
    else:
        e = ("fail", f"{opr:.0%} unique open rate — very low, hurting reputation.")

    return [
        Check("bounce_rate", "Bounce rate", b[0], b[1], 15),
        Check("complaint_rate", "Complaint rate", c[0], c[1], 15),
        Check("engagement", "Open engagement", e[0], e[1], 5),
    ]


def _reputation_check(account) -> Check:
    rep = getattr(account, "send_reputation", None)
    state = getattr(rep, "state", "ok")
    if state == "halted":
        return Check("sending_reputation", "Sending reputation", "fail",
                     f"Sending is HALTED: {getattr(rep, 'halted_reason', '') or 'reputation threshold crossed'}.", 10)
    if state == "warned":
        return Check("sending_reputation", "Sending reputation", "warn",
                     "Reputation is in a warning state — bounce/complaint volume is elevated.", 10)
    return Check("sending_reputation", "Sending reputation", "ok", "No reputation alerts.", 10)


def _list_quality_check(account, domain) -> Check:
    week_ago = timezone.now() - timedelta(days=7)
    supp = SuppressionListEntry.objects.filter(account=account, created_at__gte=week_ago)
    new_supp = supp.count()
    r = rates_for(account, domain=domain, since_days=7)
    sent_week = r["volume"]

    if sent_week < _MIN_VOLUME_FOR_RATES:
        return Check("list_quality", "List quality", "na",
                     "Not enough recent volume to assess list hygiene.", 5)

    ratio = new_supp / sent_week if sent_week else 0.0
    if ratio < 0.01:
        return Check("list_quality", "List quality", "ok",
                     f"{new_supp} new suppressions this week ({ratio:.1%} of volume).", 5)
    if ratio < 0.03:
        return Check("list_quality", "List quality", "warn",
                     f"{new_supp} new suppressions this week ({ratio:.1%}) — trending up.", 5)
    return Check("list_quality", "List quality", "fail",
                 f"{new_supp} new suppressions this week ({ratio:.1%}) — audit your most recent imports.", 5)


def _recommendations(checks: list[Check], account, domain) -> list[str]:
    recs: list[str] = []
    by_key = {c.key: c for c in checks}

    for key in ("spf", "dkim", "dmarc"):
        c = by_key.get(key)
        if c and c.status == "fail":
            recs.append(f"Fix {key.upper()}: {c.detail} See the domain's DNS records page.")
    if (c := by_key.get("dmarc_policy")) and c.status == "warn":
        recs.append("Tighten DMARC to p=quarantine once a week of aggregate reports looks clean.")
    if (c := by_key.get("bounce_rate")) and c.status in ("warn", "fail"):
        recs.append("Clean your contact list: remove addresses that have hard-bounced and stop importing unverified lists.")
    if (c := by_key.get("complaint_rate")) and c.status in ("warn", "fail"):
        recs.append("Only send to recipients who opted in recently, and make unsubscribe obvious in every email.")
    if (c := by_key.get("list_quality")) and c.status in ("warn", "fail"):
        recs.append("Review the contacts imported most recently — a spike in suppressions usually traces to one bad batch.")
    if (c := by_key.get("sending_reputation")) and c.status == "fail":
        recs.append("Sending is halted. Resolve the underlying bounce/complaint issue, then ask an operator to reset your reputation.")

    # Week-over-week bounce delta from snapshots.
    prev = (
        DeliverabilitySnapshot.objects.filter(account=account, domain=domain)
        .filter(day__lte=(timezone.now() - timedelta(days=6)).date())
        .order_by("-day")
        .first()
    )
    if prev:
        prev_bounce = next(
            (c for c in prev.checks if c.get("key") == "bounce_rate" and c.get("status") != "na"),
            None,
        )
        cur_bounce = by_key.get("bounce_rate")
        if prev_bounce and cur_bounce and cur_bounce.status != "na":
            m_prev = re.search(r"([\d.]+)%", prev_bounce.get("detail", ""))
            m_cur = re.search(r"([\d.]+)%", cur_bounce.detail)
            if m_prev and m_cur:
                p, n = float(m_prev.group(1)), float(m_cur.group(1))
                if p > 0 and n > p * 1.25:
                    pct = (n - p) / p * 100
                    recs.append(
                        f"Bounce rate rose {pct:.0f}% versus last week — check contacts added since then."
                    )
    return recs


def compute_score(account, domain: EmailDomain | None = None) -> Score:
    """Compute the deliverability score for one domain, or the whole account (domain=None)."""
    if domain is not None:
        domains = [domain] if domain.status == EmailDomain.Status.VERIFIED else []
    else:
        domains = list(
            EmailDomain.objects.filter(account=account, status=EmailDomain.Status.VERIFIED)
        )

    checks: list[Check] = []
    checks += _auth_checks(domains)
    checks += _rate_checks(account, domain)
    checks.append(_reputation_check(account))
    checks.append(_list_quality_check(account, domain))

    scored = [c for c in checks if c.status in _FACTOR]
    total_weight = sum(c.weight for c in scored)
    if total_weight == 0:
        score = 0
    else:
        earned = sum(c.weight * _FACTOR[c.status] for c in scored)
        score = round(earned / total_weight * 100)

    return Score(
        score=score,
        grade=_grade(score),
        checks=checks,
        recommendations=_recommendations(checks, account, domain),
    )


def snapshot(account, domain: EmailDomain | None = None) -> DeliverabilitySnapshot:
    result = compute_score(account, domain)
    obj, _ = DeliverabilitySnapshot.objects.update_or_create(
        account=account,
        domain=domain,
        day=timezone.now().date(),
        defaults={
            "score": result.score,
            "grade": result.grade,
            "checks": [c.as_dict() for c in result.checks],
        },
    )
    return obj
