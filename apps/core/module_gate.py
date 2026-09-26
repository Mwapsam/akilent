"""View gate for plan features (see ``apps.billing.api.entitled`` / ``usable``)."""
from functools import wraps

from django.shortcuts import redirect
from django.urls import reverse

from apps.accounts.utils import get_current_account
from apps.billing import api as billing_api
from apps.billing import features as catalog


def module_required(feature: str):
    """Send the business to the feature's locked page when it can't use ``feature`` here.

    ``feature`` is a catalog key (``apps.billing.features``). Not entitled, or switched off by
    the owner, both land on ``/billing/locked/<key>/``, which explains which one it is. Apply
    below ``@login_required``. Requests with no current account fall through to the view.
    """
    catalog.get(feature)  # an unknown key fails at import time, not on the first request

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            account = get_current_account(request)
            if account is not None and not billing_api.usable(account, feature):
                return redirect(reverse("billing:locked", args=[feature]))
            return view(request, *args, **kwargs)
        return wrapper
    return decorator
