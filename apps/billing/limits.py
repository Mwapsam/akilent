"""LimitChecker: the older per-account checks, now thin shims over ``apps.billing.api``.

Features come from the catalog (``entitled``) and limits from the limit catalog (``limit`` /
``reserve``); nothing here reads Plan columns. The email checks keep their original rule that the
subscription must be active (trialing or paid).
"""
import logging

logger = logging.getLogger(__name__)

EMAIL_LIMITS = ["emails_month", "emails_day"]


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

    def _require_room(self, key: str, limit_type: str):
        from apps.billing import api as billing_api

        self._require_active_plan()
        try:
            billing_api.require_room(self.account, key)
        except billing_api.LimitReached as exc:
            raise PlanLimitExceeded(str(exc), limit_type) from exc

    def check_whatsapp_number(self):
        self._require_room("whatsapp_numbers", "whatsapp_numbers")

    def check_automation_rule(self):
        self._require_room("automation_rules", "automation_rules")

    def has_feature(self, name: str) -> bool:
        """Whether the active plan includes one of the old email capabilities.

        ``name`` is an old Plan column name (``features.LEGACY_FLAGS``), answered from the
        feature catalog.
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

    def check_email(self, operation_id: str):
        """Reserve one email (this month and today) for ``operation_id``, atomically.

        Checks *and* claims in one step. The send path settles it: ``settle_email`` commits on
        success and releases on a permanent failure. Reserving the same operation again (a retry)
        doesn't count twice.
        """
        from apps.billing import api as billing_api

        self._require_active_plan()
        taken, blocked = billing_api.reserve_all_verbose(self.account, EMAIL_LIMITS, operation_id=operation_id)
        if taken is None:
            # Named from the reservation attempt itself, not a re-query after the fact: usage can
            # move between the failed reserve and a later check, and would then name the wrong limit.
            which = ("Daily email limit" if blocked == "emails_day" else "Monthly email limit")
            limit_value = billing_api.limit(self.account, blocked)
            raise PlanLimitExceeded(f"{which} of {limit_value} reached. Please upgrade your plan.", "emails")

    def settle_email(self, operation_id: str, *, ok: bool) -> None:
        """Commit (sent) or release (never delivered) an email's reservation."""
        from apps.billing import api as billing_api

        billing_api.settle_operation(self.account, operation_id, ok=ok)
