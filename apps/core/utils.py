from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def is_operator(user) -> bool:
    """A platform operator: runs Akilent itself, not a business on it. The one admin check."""
    return bool(user and user.is_authenticated and user.is_superuser)



def admin_required(view):
    """Gate an Operator Console view. Anonymous users go to log in; a logged-in business user
    gets a 403, not a login loop, since logging in again wouldn't help."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not is_operator(request.user):
            raise PermissionDenied("Operator Console only.")
        return view(request, *args, **kwargs)

    return wrapper
