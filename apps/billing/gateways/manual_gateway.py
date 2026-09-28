from django.shortcuts import render

from ..models import PaymentMethod
from .base import PaymentGateway


class ManualGateway(PaymentGateway):
    """Offline payment — the tenant is shown instructions and submits a
    reference for an admin to review, instead of being redirected anywhere."""

    code = "manual"
    label = "Bank transfer / offline"

    def start_checkout(self, request, account, plan):
        method = PaymentMethod.objects.filter(code=self.code).first()
        return render(
            request,
            "billing/manual_checkout.html",
            {
                "plan": plan,
                "account": account,
                "instructions": method.instructions if method else "",
            },
        )
