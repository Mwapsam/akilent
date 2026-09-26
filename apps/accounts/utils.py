"""Helpers for resolving the account a logged-in user is currently acting on."""

from apps.accounts.models import Account, Membership

_SESSION_KEY = "current_account_id"
# Memoised per request. A view and both account context processors each ask for
# the current account, so without this every authenticated page paid for the
# same membership lookup three or more times. Safe because Django builds a fresh
# HttpRequest per request, and _SESSION_KEY is only ever written by
# set_current_account below, which keeps the cache in step.
_CACHE_ATTR = "_current_account"


def get_current_account(request):
    """Return the ``Account`` the request's user is currently scoped to.

    Resolution order:
      1. The account id pinned in the session (if the user still belongs to it).
      2. The user's owner membership, else their first membership.
    Returns ``None`` for anonymous users or users without any membership.

    Memoised on the request; ``set_current_account`` refreshes the memo.
    """
    if hasattr(request, _CACHE_ATTR):
        return getattr(request, _CACHE_ATTR)

    account = _resolve_current_account(request)
    setattr(request, _CACHE_ATTR, account)
    return account


def _resolve_current_account(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return None

    viewed = viewing_as(request)
    if viewed is not None:
        return viewed

    # A suspended business (Account.is_active=False) resolves to no workspace, so its pages,
    # sends and settings stop working; SuspendedAccountMiddleware tells its members why.
    memberships = Membership.objects.filter(user=user, account__is_active=True).select_related("account")

    pinned = request.session.get(_SESSION_KEY)
    if pinned:
        for m in memberships:
            if m.account_id == pinned:
                return m.account

    owner = memberships.filter(role=Membership.Role.OWNER).first()
    membership = owner or memberships.first()
    if membership is None:
        return None

    set_current_account(request, membership.account)
    return membership.account


def set_current_account(request, account: Account) -> None:
    request.session[_SESSION_KEY] = account.pk
    # Keep the memo honest - switching workspace mid-request must be visible to
    # anything that asks for the current account afterwards.
    setattr(request, _CACHE_ATTR, account)


# --- Read-only "View as" (support) --------------------------------------------------------------
# An operator can look at a business's own screens. The session holds which business and since
# when; apps.core.middleware.ViewAsReadOnlyMiddleware blocks every change while it's on.
VIEW_AS_KEY = "viewing_as"
VIEW_AS_MINUTES = 60


def viewing_as(request):
    """The business an operator is viewing as, or None (also None once it has expired)."""
    from datetime import timedelta

    from django.utils import timezone
    from django.utils.dateparse import parse_datetime

    from apps.core.utils import is_operator

    state = request.session.get(VIEW_AS_KEY) if hasattr(request, "session") else None
    if not state or not is_operator(getattr(request, "user", None)):
        return None
    started = parse_datetime(state.get("started") or "")
    if started is None or timezone.now() - started > timedelta(minutes=VIEW_AS_MINUTES):
        request.session.pop(VIEW_AS_KEY, None)
        return None
    return Account.objects.filter(pk=state.get("account")).first()


def start_view_as(request, account) -> None:
    from django.utils import timezone

    request.session[VIEW_AS_KEY] = {"account": account.pk, "started": timezone.now().isoformat()}
    setattr(request, _CACHE_ATTR, account)


def stop_view_as(request):
    """End a View-as session. Returns the account that was being viewed, if any."""
    state = request.session.pop(VIEW_AS_KEY, None) or {}
    if hasattr(request, _CACHE_ATTR):
        delattr(request, _CACHE_ATTR)
    return Account.objects.filter(pk=state.get("account")).first() if state else None


def is_suspended_member(request) -> bool:
    """A logged-in person whose only workspaces are suspended (and who isn't viewing as someone)."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return False
    memberships = Membership.objects.filter(user=user)
    return memberships.exists() and not memberships.filter(account__is_active=True).exists()


def user_accounts(user):
    return Account.objects.filter(memberships__user=user).distinct()


def is_ajax(request) -> bool:
    """Was this request made in the background rather than as a navigation?

    Accepts both markers so a view can serve the hand-rolled ``data-ajax``
    engine and HTMX at once: templates move across one at a time, and the view
    behind them does not have to move on the same commit. ``HX-Request`` is set
    by HTMX on every request it makes; ``X-Requested-With`` is what
    ``handleAjaxForm`` in static/js/app.js sends.
    """
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.headers.get("hx-request") == "true"
    )


def ajax_redirect(url: str):
    """Tell a background request to navigate the browser to ``url``.

    Speaks to both background clients at once. The hand-rolled engine in
    static/js/app.js reads ``{"redirect": ...}`` out of the JSON body; HTMX
    ignores the body entirely and acts on the ``HX-Redirect`` header. One
    response therefore works whether or not the calling template has moved
    across to hx-* yet.
    """
    from django.http import JsonResponse

    response = JsonResponse({"redirect": url})
    response["HX-Redirect"] = url
    return response
