"""View gate for tenant modules (see ``apps.billing.api.module_enabled``)."""
from functools import wraps

from django.contrib import messages
from django.shortcuts import redirect

from apps.accounts.utils import get_current_account
from apps.billing import api as billing_api


def module_required(feature: str):
    """Redirect to the dashboard when ``feature`` is disabled for the current account.

    Apply below ``@login_required``. Requests with no current account fall through
    to the view's own handling.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            account = get_current_account(request)
            if account is not None and not billing_api.module_enabled(account, feature):
                messages.error(request, "This feature isn't enabled for your account.")
                return redirect("dashboard")
            return view(request, *args, **kwargs)
        return wrapper
    return decorator
