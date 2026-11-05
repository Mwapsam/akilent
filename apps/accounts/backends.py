from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied


class EmailBackend(ModelBackend):
    """Authenticate by email address instead of username.

    Django's ``AuthenticationForm`` always submits the credential under the
    ``username`` kwarg, so we accept it under that name and just treat the
    value as an email. ``ModelBackend`` stays registered alongside this one
    (see AUTHENTICATION_BACKENDS) so admin/username-based auth keeps working.

    Every attempt costs exactly one password hash. Hashing is deliberately slow,
    and a second backend hashing again doubled the time of every failed sign-in:

    - The email matches an account: we check the password and, on failure, raise
      PermissionDenied so Django stops without trying ModelBackend.
    - No email match: we return None without hashing. ModelBackend then checks the
      username, or runs its own dummy hash, so the time stays the same whether or
      not the account exists.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        try:
            user = User._default_manager.get(email__iexact=username)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        raise PermissionDenied

    def get_user(self, user_id):
        """The signed-in person, with their session generation in the same query (a join), so
        checking it on every request (apps.core.sessions) adds no query."""
        try:
            user = User._default_manager.select_related("session_security").get(
                pk=user_id
            )
        except User.DoesNotExist:
            return None
        return user if self.user_can_authenticate(user) else None
