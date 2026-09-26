import logging

logger = logging.getLogger(__name__)


class PlanLimitExceeded(Exception):
    def __init__(self, message: str, limit_type: str):
        self.limit_type = limit_type
        super().__init__(message)


class LimitChecker:
    def __init__(self, account):
        self.account = account
        try:
            self.subscription = account.subscription
        except Exception:
            self.subscription = None

    def _require_active_plan(self):
        if not self.subscription or not self.subscription.is_active:
            raise PlanLimitExceeded(
                "No active subscription. Please subscribe to a plan.",
                "subscription",
            )
        return self.subscription.plan

    def check_conversation(self):
        plan = self._require_active_plan()
        if plan.max_conversations_per_month == -1:
            return
        from apps.billing.models import UsageSummary
        used = UsageSummary.get_current_usage(self.account)
        if used >= plan.max_conversations_per_month:
            raise PlanLimitExceeded(
                f"Monthly conversation limit of {plan.max_conversations_per_month} reached. "
                "Please upgrade your plan.",
                "conversations",
            )

    def check_whatsapp_number(self):
        plan = self._require_active_plan()
        if plan.max_whatsapp_numbers == -1:
            return
        from apps.whatsapp import api as whatsapp_api
        count = whatsapp_api.count_active_business_numbers(self.account)
        if count >= plan.max_whatsapp_numbers:
            raise PlanLimitExceeded(
                f"WhatsApp number limit of {plan.max_whatsapp_numbers} reached. "
                "Upgrade to a higher plan to add more numbers.",
                "whatsapp_numbers",
            )

    def check_automation_rule(self):
        plan = self._require_active_plan()
        if plan.max_automation_rules == -1:
            return
        from apps.automation import api as automation_api
        count = automation_api.count_active_rules(self.account)
        if count >= plan.max_automation_rules:
            raise PlanLimitExceeded(
                f"Automation rule limit of {plan.max_automation_rules} reached. "
                "Upgrade your plan to add more rules.",
                "automation_rules",
            )

    def has_feature(self, name: str) -> bool:
        """Whether the active plan includes one of the old email capabilities.

        A shim over ``billing_api.entitled``: ``name`` is an old Plan column name
        (``features.LEGACY_FLAGS``), answered from the feature catalog. Keeps its original rule
        that the subscription must be active (trialing or paid).
        """
        from apps.billing import api as billing_api
        from apps.billing.features import LEGACY_FLAGS

        if not self.subscription or not self.subscription.is_active:
            return False
        return billing_api.entitled(self.account, LEGACY_FLAGS.get(name, name))

    def require_feature(self, name: str, label: str = ""):
        if not self.has_feature(name):
            raise PlanLimitExceeded(
                f"Your plan does not include {label or name}. Upgrade to enable it.",
                name,
            )

    def check_email(self):
        """Reserve one email against the monthly cap, atomically.

        Unlike the other check_* methods this both checks *and* claims the
        slot in one step — callers that reserve at accept time must call
        release_email() if the send later fails terminally, to avoid
        permanently burning quota on a message that was never delivered.
        """
        plan = self._require_active_plan()
        from apps.billing.models import UsageSummary
        if not UsageSummary.reserve_email(self.account, plan.max_emails_per_month):
            raise PlanLimitExceeded(
                f"Monthly email limit of {plan.max_emails_per_month} reached. "
                "Please upgrade your plan.",
                "emails",
            )

    def release_email(self):
        from apps.billing.models import UsageSummary
        UsageSummary.release_email(self.account)

    def reserve_bulk(self, count: int) -> int:
        """Reserve up to `count` emails against the monthly cap for a bulk

        campaign chunk. Returns how many were actually reserved (partial-send
        semantics — see UsageSummary.reserve_email_bulk).
        """
        plan = self._require_active_plan()
        from apps.billing.models import UsageSummary
        return UsageSummary.reserve_email_bulk(
            self.account, plan.max_emails_per_month, count
        )
