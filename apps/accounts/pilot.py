"""Pilot enrollment helpers for Phase 5 — soft launch tracking.

setup_score() measures how completely a business has configured Akilent.
enroll_pilot() gates on ≥80 and snapshots baseline metrics at enrollment.
pilot_outcome() returns current vs baseline for each enrolled pilot.
"""

from __future__ import annotations

_SETUP_SCORE_MINIMUM = 80


def setup_score(account) -> int:
    """Return a 0-100 completion score for *account*.

    Score components:

    BusinessContext (40 pts)
      business_model set      +10
      customer_channels set   +10
      objectives set          +15
      capability_profile set   +5

    BusinessKnowledge (30 pts)
      who_is_it_for / problem_solved  +15
      common_questions / faqs         +10
      eligibility_rules / recommended  +5

    BusinessProfile (10 pts)
      what_you_sell set   +5
      location set        +5

    Operational readiness (20 pts)
      WhatsApp number configured  +10
      Completed campaign exists    +5
      Team size ≥ 2 members        +5

    Gate: score ≥ 80 → pilot-ready.
    """
    score = 0

    try:
        ctx = account.business_context
        if ctx.business_model:
            score += 10
        if ctx.customer_channels:
            score += 10
        if ctx.objectives:
            score += 15
        if ctx.capability_profile:
            score += 5
    except Exception:
        pass

    try:
        knowledge = account.business_knowledge
        if knowledge.who_is_it_for or knowledge.problem_solved:
            score += 15
        if knowledge.common_questions or knowledge.faqs:
            score += 10
        if knowledge.eligibility_rules or knowledge.recommended_for:
            score += 5
    except Exception:
        pass

    try:
        profile = account.business_profile
        if profile.what_you_sell:
            score += 5
        if profile.location:
            score += 5
    except Exception:
        pass

    from apps.whatsapp.models import WhatsAppBusinessNumber, WhatsAppCampaign

    if WhatsAppBusinessNumber.objects.filter(account=account).exists():
        score += 10

    if WhatsAppCampaign.objects.filter(
        account=account, status=WhatsAppCampaign.Status.COMPLETED
    ).exists():
        score += 5

    if account.memberships.count() >= 2:
        score += 5

    return score


def enroll_pilot(account, *, wave: int = 2, enrolled_by=None):
    """Enroll *account* as a pilot and snapshot baseline metrics.

    Raises ValueError if setup_score(account) < 80.
    Returns the PilotEnrollment instance (created or updated).
    """
    from apps.accounts.models import PilotEnrollment

    score = setup_score(account)
    if score < _SETUP_SCORE_MINIMUM:
        raise ValueError(
            f"Account {account.slug!r} setup score {score} is below the required "
            f"{_SETUP_SCORE_MINIMUM}. Complete the Business Context setup before enrolling."
        )

    from apps.conversations.state import average_first_response_seconds
    from apps.crm.models import Lead

    baseline_avg_response = average_first_response_seconds(account)
    baseline_leads = Lead.objects.filter(account=account).count()

    try:
        from apps.conversations.models import Conversation

        baseline_conversations = Conversation.objects.filter(account=account).count()
    except Exception:
        baseline_conversations = 0

    enrollment, _ = PilotEnrollment.objects.update_or_create(
        account=account,
        defaults={
            "wave": wave,
            "enrolled_by": enrolled_by,
            "baseline_avg_response_seconds": baseline_avg_response,
            "baseline_lead_count": baseline_leads,
            "baseline_conversation_count": baseline_conversations,
        },
    )
    return enrollment


def pilot_outcome(account) -> dict:
    """Return a dict comparing current metrics vs the enrollment baseline.

    Returns {"enrolled": False} if the account has not been enrolled.
    """
    from apps.accounts.models import PilotEnrollment

    try:
        enrollment = account.pilot_enrollment
    except PilotEnrollment.DoesNotExist:
        return {"enrolled": False}

    from apps.conversations.state import average_first_response_seconds
    from apps.crm.models import Lead

    current_avg_response = average_first_response_seconds(account)
    current_leads = Lead.objects.filter(account=account).count()

    try:
        from apps.conversations.models import Conversation

        current_conversations = Conversation.objects.filter(account=account).count()
    except Exception:
        current_conversations = None

    result: dict = {
        "enrolled": True,
        "wave": enrollment.wave,
        "enrolled_at": enrollment.enrolled_at.isoformat(),
        "setup_score": setup_score(account),
        "baseline_avg_response_seconds": enrollment.baseline_avg_response_seconds,
        "current_avg_response_seconds": current_avg_response,
        "baseline_lead_count": enrollment.baseline_lead_count,
        "current_lead_count": current_leads,
        "leads_added": current_leads - enrollment.baseline_lead_count,
        "baseline_conversation_count": enrollment.baseline_conversation_count,
        "current_conversation_count": current_conversations,
    }

    if enrollment.baseline_avg_response_seconds and current_avg_response:
        improvement = (
            (enrollment.baseline_avg_response_seconds - current_avg_response)
            / enrollment.baseline_avg_response_seconds
            * 100
        )
        result["response_time_improvement_pct"] = round(improvement, 1)

    return result
