"""``{% next_action_card %}``: the one next step in the journey, on the pages a business
works from (dashboard, inbox, Content Studio). See onboarding.next_action."""

from django import template

register = template.Library()


@register.inclusion_tag("components/next_action.html", takes_context=True)
def next_action_card(context):
    request = context.get("request")
    action = None
    if request is not None and request.user.is_authenticated:
        from apps.accounts import onboarding as ob
        from apps.accounts.utils import get_current_account, viewing_as

        account = get_current_account(request)
        # Never for an operator looking at someone else's workspace.
        if account is not None and viewing_as(request) is None:
            try:
                action = ob.next_action(account, request=request)
            except Exception:  # a suggestion must never break the page
                import logging

                logging.getLogger(__name__).exception("next_action failed")
    return {"action": action, "request": request}
