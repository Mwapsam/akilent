import logging
import uuid

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect

from ..flutterwave import FlutterwaveError, get_fw_client
from .base import PaymentGateway

logger = logging.getLogger(__name__)


class FlutterwaveGateway(PaymentGateway):
    code = "flutterwave"
    label = "Card (Flutterwave)"

    def start_checkout(self, request, account, plan):
        tx_ref = f"sub_{account.pk}_{plan.slug}_{uuid.uuid4().hex[:8]}"
        currency = getattr(settings, "FLUTTERWAVE_CURRENCY", "USD")
        redirect_url = request.build_absolute_uri("/billing/callback/")

        try:
            fw = get_fw_client()
            # Ensure a recurring payment plan exists so the charge auto-renews monthly.
            if not plan.flutterwave_plan_id:
                fp = fw.create_payment_plan(
                    name=plan.name,
                    amount=plan.price_monthly,
                    interval="monthly",
                    currency=currency,
                )
                plan.flutterwave_plan_id = str(fp.get("id") or "")
                plan.save(update_fields=["flutterwave_plan_id"])
            link = fw.initialize_payment(
                tx_ref=tx_ref,
                amount=plan.price_monthly,
                currency=currency,
                customer_email=request.user.email
                or f"admin+{account.pk}@automator.local",
                customer_name=account.company_name or request.user.username,
                redirect_url=redirect_url,
                payment_plan_id=plan.flutterwave_plan_id,
                meta={"account_id": account.pk, "plan_slug": plan.slug},
            )
        except FlutterwaveError as exc:
            logger.error(
                "checkout: FW error for account=%s plan=%s: %s",
                account.pk,
                plan.slug,
                exc,
            )
            messages.error(request, f"Payment initialization failed: {exc}")
            return redirect("/billing/plans/")

        request.session["pending_tx_ref"] = tx_ref
        request.session["pending_account_id"] = account.pk
        request.session["pending_plan_slug"] = plan.slug

        return redirect(link)
