from rest_framework.permissions import BasePermission

from apps.billing.limits import LimitChecker


class HasEmailApiFeature(BasePermission):
    """Gate on the account's plan including the email API & SMTP relay feature.

    request.user is the authenticated Account (see EmailApiKeyAuthentication);
    this is a plan-tier gate, distinct from ApiKeyRateThrottle's per-minute
    request-rate limiting and LimitChecker.check_email()'s monthly volume cap.
    """

    message = "Your plan does not include the email API & SMTP relay. Upgrade to send."

    def has_permission(self, request, view) -> bool:
        account = request.user
        if account is None:
            return False
        return LimitChecker(account).has_feature("email_apis")


class HasAutomationModule(BasePermission):
    """The tenant's Automation module must not be explicitly disabled."""

    message = "The automation module is not enabled for this account."

    def has_permission(self, request, view) -> bool:
        from apps.billing import api as billing_api

        account = request.user
        return account is not None and billing_api.module_enabled(account, "automation")


class HasBulkEmailFeature(BasePermission):
    """Gate on the account's plan including bulk/campaign sending."""

    message = "Your plan does not include bulk email sending. Upgrade to send campaigns."

    def has_permission(self, request, view) -> bool:
        account = request.user
        if account is None:
            return False
        return LimitChecker(account).has_feature("bulk_email")


class HasEmailTemplatesFeature(BasePermission):
    """Gate on the account's plan including reusable email templates."""

    message = "Your plan does not include email templates."

    def has_permission(self, request, view) -> bool:
        account = request.user
        if account is None:
            return False
        return LimitChecker(account).has_feature("email_templates")


class HasScope(BasePermission):
    """Require a specific scope on the authenticating EmailApiKey.

    request.auth is the EmailApiKey (see EmailApiKeyAuthentication).
    EmailApiKey.scopes has historically been stored but never enforced;
    applied only to newly added endpoints so existing keys (which all
    default to scopes=["messages:send"]) keep working against existing
    endpoints unchanged.
    """

    required_scope = ""

    def has_permission(self, request, view) -> bool:
        api_key = request.auth
        if api_key is None:
            return False
        required = getattr(view, "required_scope", self.required_scope)
        return required in (api_key.scopes or [])
