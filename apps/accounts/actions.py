"""Account actions for the shared Action Registry. Read-only: they look things up, never change them."""
from __future__ import annotations

from apps.core.actions import Action, register


class LookupBusinessHoursAction(Action):
    """Is the business open right now, and if not, when does it next open?"""

    name = "lookup_business_hours"
    scope_kwarg = "account"

    def input_schema(self) -> dict:
        return {"required": ["account"]}

    def execute(self, context: dict, *, account) -> dict:
        from apps.accounts.business_hours import availability

        return availability(account)


register(LookupBusinessHoursAction())
