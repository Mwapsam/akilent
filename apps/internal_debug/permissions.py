from rest_framework.permissions import BasePermission


class IsInternalDebugAuthenticated(BasePermission):
    """True once InternalDebugTokenAuthentication has set request.auth.

    Not IsAuthenticated: there's no Django User here, so request.user is a
    plain string placeholder rather than an object with is_authenticated.
    """

    def has_permission(self, request, view):
        return bool(request.auth)
