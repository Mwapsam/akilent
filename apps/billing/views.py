import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.accounts.utils import ajax_redirect, get_current_account, is_ajax
from apps.core.audit import audit
from apps.core.models import SiteSettings
from apps.core.utils import admin_required

from . import credit, payments
from .models import (
    BillingPayment,
    ManualPaymentRequest,
    PaymentMethod,
    Plan,
    ProcessedWebhookEvent,
    Subscription,
)
from .pricing import DEFAULT_PERIOD_MULTIPLIERS, PERIOD_MONTH_MULTIPLES
from .providers import InvalidSignature, ProviderError, registry
from .services import activate_subscription

logger = logging.getLogger(__name__)


@login_required
def pricing_page(request):
    """The business's own plans page. Operators manage packages, payment methods and bank
    transfers in the Operator console (/manage/plans/, /manage/payments/), not here."""
    account = get_current_account(request)
    if account is None:
        return redirect("/dashboard/")

    from apps.billing import api as billing_api

    plans = list(Plan.objects.filter(is_active=True).order_by("price_monthly"))
    from apps.core.channels import enabled_channels, limit_visible

    channels = enabled_channels()
    subscription = getattr(account, "subscription", None) if account else None
    current_plan_slug = subscription.plan.slug if subscription else None
    site = SiteSettings.load()

    period_pricing = {
        period: {
            "label": label,
            "months": float(PERIOD_MONTH_MULTIPLES[period]),
            "multiplier": float(DEFAULT_PERIOD_MULTIPLIERS[period]),
        }
        for period, label in Subscription.BILLING_PERIOD_CHOICES
    }

    return render(
        request,
        "billing/plans.html",
        {
            "plans": plans,
            "cards": [billing_api.plan_card(p, channels=channels) for p in plans],
            "compare_rows": billing_api.compare_plans(plans, channels=channels),
            "core_features": billing_api.core_features(),
            "account": account,
            "subscription": subscription,
            "current_plan_slug": current_plan_slug,
            "plan_service_type_choices": Plan.SERVICE_TYPE_CHOICES,
            "usage": [
                u
                for u in billing_api.usage_report(account)
                if u["period"] != "per_use" and limit_visible(u["key"], channels)
            ],
            "usage_warnings": billing_api.usage_warnings(account),
            "payments_enabled": site.payments_enabled,
            "payment_methods": checkout_options() if site.payments_enabled else [],
            "credit": credit.summary(account),
            "billing_periods": Subscription.BILLING_PERIOD_CHOICES,
            "period_pricing_json": json.dumps(period_pricing),
        },
    )


@login_required
def feature_locked(request, key):
    """Why this business can't use a feature, what it does, and where to get it. A product state,
    not an error: nothing the business made with the feature is deleted."""
    from django.http import Http404

    from apps.billing import api as billing_api
    from apps.billing import features as catalog

    account = get_current_account(request)
    if account is None:
        return redirect("/dashboard/")
    feature = catalog.BY_KEY.get(key)
    if feature is None or feature.availability in (
        catalog.INTERNAL,
        catalog.DEPRECATED,
    ):
        raise Http404
    info = billing_api.access(account, key)
    kept = ""
    if key == "automations":
        from apps.automation import api as automation_api

        count = automation_api.count_active_rules(account)
        if count:
            kept = (
                f"Your {count} automation{'s are' if count != 1 else ' is'} kept. "
                "They don't run while this is locked, and start again when you have access."
            )
    return render(
        request, "billing/locked.html", {"info": info, "kept": kept, "feature": feature}
    )


# --- Admin: package (Plan) management -----------------------------------------


def _plan_form_fields(post):
    """Pull + coerce Plan fields from POST (shared by create/edit)."""

    def _int(name, default=0):
        try:
            return int(post.get(name, default) or default)
        except ValueError:
            return default

    from decimal import Decimal, InvalidOperation

    try:
        price = Decimal(post.get("price_monthly") or "0")
    except InvalidOperation:
        price = Decimal("0")
    service_type = post.get("service_type") or Plan.SERVICE_EMAIL
    if service_type not in dict(Plan.SERVICE_TYPE_CHOICES):
        service_type = Plan.SERVICE_EMAIL
    return {
        "name": (post.get("name") or "").strip(),
        "service_type": service_type,
        "price_monthly": price,
        # Limits are set in /manage/plans/limits/ (PlanLimit), features in /manage/plans/features/.
        "trial_days": _int("trial_days"),
        "log_retention_days": _int("log_retention_days"),
        # What a plan includes is set in the feature matrix (/manage/plans/features/).
        "is_active": "is_active" in post,
    }


@admin_required
@require_POST
def plan_create(request):
    from django.utils.text import slugify

    fields = _plan_form_fields(request.POST)
    slug = slugify(request.POST.get("slug") or fields["name"])
    if not slug or not fields["name"]:
        messages.error(request, "Package name (and slug) are required.")
        return redirect("core:plans")
    if Plan.objects.filter(slug=slug).exists():
        messages.error(request, f"A package with slug '{slug}' already exists.")
        return redirect("core:plans")
    # One transaction: a concurrent reader never sees the plan active before the loss check runs,
    # since an uncommitted row isn't visible outside this transaction.
    with transaction.atomic():
        plan = Plan.objects.create(slug=slug, **fields)
        audit(request, "plan.create", target=slug)
        reasons = _loss_check(request, plan)
        if reasons:
            plan.is_active = False
            plan.save(update_fields=["is_active"])
    if reasons:
        messages.warning(
            request,
            f"Package '{plan.name}' created but hidden: {' '.join(reasons)} "
            'Set its limits, or edit it and tick "I accept".',
        )
        return redirect("core:plans")
    messages.success(request, f"Package '{fields['name']}' created.")
    return redirect("core:plans")


def _loss_check(request, plan, **changes) -> list:
    """Reasons a paid, listed plan could lose money (billing_api.loss_reasons), unless the operator
    ticked "I accept this plan can lose money" - then it's recorded and allowed."""
    from apps.billing import api as billing_api

    reasons = billing_api.loss_reasons(plan, **changes)
    if reasons and request.POST.get("accept_loss") == "on":
        audit(request, "plan.loss_acknowledged", target=plan.slug, reasons=reasons)
        return []
    return reasons


@admin_required
@require_POST
def plan_edit(request, pk):
    plan = get_object_or_404(Plan, pk=pk)
    fields = _plan_form_fields(request.POST)
    if not fields["name"]:
        messages.error(request, "Package name is required.")
        return redirect("core:plans")
    reasons = _loss_check(
        request, plan, price=fields["price_monthly"], is_active=fields["is_active"]
    )
    if reasons:
        messages.error(
            request,
            f'Not saved: {" ".join(reasons)} Tick "I accept this plan can lose money" to save it anyway.',
        )
        return redirect("core:plans")
    for k, v in fields.items():
        setattr(plan, k, v)
    plan.save()
    audit(request, "plan.edit", target=plan.slug)
    messages.success(request, f"Package '{plan.name}' updated.")
    return redirect("core:plans")


@admin_required
@require_POST
def plan_toggle(request, pk):
    plan = get_object_or_404(Plan, pk=pk)
    if not plan.is_active:
        reasons = _loss_check(request, plan, is_active=True)
        if reasons:
            messages.error(
                request,
                f"'{plan.name}' stays hidden: {' '.join(reasons)} "
                'Edit it and tick "I accept" to show it anyway.',
            )
            return redirect("core:plans")
    plan.is_active = not plan.is_active
    plan.save(update_fields=["is_active"])
    audit(request, "plan.toggle", target=plan.slug, is_active=plan.is_active)
    messages.success(
        request,
        f"Package '{plan.name}' {'activated' if plan.is_active else 'deactivated'}.",
    )
    return redirect("core:plans")


@admin_required
@require_POST
def plan_delete(request, pk):
    plan = get_object_or_404(Plan, pk=pk)
    if plan.subscriptions.exists():
        messages.error(
            request,
            f"Can't delete '{plan.name}' — customers are subscribed. Deactivate it instead.",
        )
        return redirect("core:plans")
    name = plan.name
    plan.delete()
    audit(request, "plan.delete", target=name)
    messages.success(request, f"Package '{name}' deleted.")
    return redirect("core:plans")


# --- Checkout and payments (provider-neutral) ---------------------------------
#
# Providers live in apps.billing.providers; nothing below knows a provider's API. A checkout
# creates a BillingPayment (credit and unused time already taken off), the active provider takes
# the customer to its hosted page, and its return and webhooks come back as ProviderResults that
# payments.record_result turns into subscription state.


def checkout_options(account=None) -> list[dict]:
    """How a business can pay right now: the active provider, plus bank transfer if it's on."""
    options = []
    provider = registry.active()
    if provider is not None:
        options.append({"code": provider.code, "name": "Pay by card"})
    if registry.manual_enabled():
        manual = PaymentMethod.objects.filter(code="manual").first()
        options.append(
            {"code": "manual", "name": manual.name if manual else "Bank transfer"}
        )
    return options


@login_required
def checkout(request):
    account = get_current_account(request)
    if account is None:
        return redirect("/dashboard/")

    if not SiteSettings.load().payments_enabled:
        messages.error(request, "Billing is currently disabled. Contact support.")
        return redirect("/billing/plans/")

    plan = get_object_or_404(Plan, slug=request.GET.get("plan"), is_active=True)
    if plan.slug == Plan.TRIAL or plan.price_monthly <= 0:
        messages.error(request, "That plan doesn't need a payment.")
        return redirect("/billing/plans/")

    period = request.GET.get("period") or Subscription.MONTHLY
    if period not in dict(Subscription.BILLING_PERIOD_CHOICES):
        messages.error(request, "Invalid billing period selected.")
        return redirect("/billing/plans/")

    if request.GET.get("method") == "manual" and registry.manual_enabled():
        provider = registry.get("manual")
    else:
        provider = registry.active()
    if provider is None:
        messages.error(
            request, "No payment method is currently available. Contact support."
        )
        return redirect("/billing/plans/")

    payments.abandon_open_checkouts(account)
    payment = payments.create_payment(account, plan, period, provider=provider.code)
    if payment.status == BillingPayment.SUCCEEDED:
        messages.success(
            request,
            f"Paid with your account credit. You're on {plan.name} until "
            f"{account.subscription.current_period_end:%d %b %Y}.",
        )
        return _after_payment(account)
    return provider.start_checkout(request, payment)


def _after_payment(account):
    from apps.accounts import onboarding as ob

    if account.onboarding_state != account.Onboarding.COMPLETED:
        return redirect(ob.first_setup_url(account))
    return redirect("/billing/plans/")


@login_required
def payment_return(request, provider):
    """Where a provider's hosted page sends the customer back. The redirect alone proves
    nothing: the provider verifies it, and only the caller's own account's payment is looked at
    (a reference for someone else's payment gets the same "not finished" page)."""
    account = get_current_account(request)
    impl = registry.get(provider)
    reference = impl.reference_from_return(request) if impl else None
    payment = (
        BillingPayment.objects.filter(
            reference=reference, account=account, provider=provider
        ).first()
        if account and reference
        else None
    )
    if payment is not None and payment.status != BillingPayment.SUCCEEDED:
        try:
            result = impl.handle_return(request, payment)
        except ProviderError as exc:
            logger.warning("payment_return: %s verify failed: %s", provider, exc)
            result = None
        if result is not None:
            payment = payments.record_result(provider, result) or payment

    if payment is not None and payment.status == BillingPayment.SUCCEEDED:
        messages.success(request, f"Payment confirmed. You're on {payment.plan.name}.")
        return _after_payment(account)
    if payment is not None and payment.status in (
        BillingPayment.FAILED,
        BillingPayment.ABANDONED,
    ):
        messages.error(
            request, "The payment didn't go through. You haven't been charged."
        )
        return redirect("/billing/plans/")
    return render(request, "billing/payment_pending.html", {"payment": payment})


@login_required
def payment_status(request, reference):
    """Polled by the pending page while a provider confirms a payment."""
    account = get_current_account(request)
    payment = get_object_or_404(BillingPayment, reference=reference, account=account)
    return JsonResponse({"status": payment.status})


@csrf_exempt
@require_POST
def provider_webhook(request, provider):
    """Webhooks for every provider. A bad signature is 401; an error while handling is 500, so
    the provider retries, and the de-dupe row rolls back with it so the retry is processed."""
    impl = registry.get(provider)
    if impl is None or not impl.has_webhooks:
        return HttpResponse(status=404)
    try:
        events = impl.parse_webhook(request)
    except InvalidSignature as exc:
        logger.warning("webhook %s: rejected: %s", provider, exc)
        return HttpResponse(status=401)
    except ProviderError as exc:
        logger.error("webhook %s: couldn't read the event: %s", provider, exc)
        return HttpResponse(status=500)

    for event in events:
        try:
            with transaction.atomic():
                _, created = ProcessedWebhookEvent.objects.get_or_create(
                    event_key=event.key[:255]
                )
                if not created:
                    logger.info("webhook %s: duplicate %s ignored", provider, event.key)
                    continue
                if event.handler is not None:
                    event.handler()
                if event.result is not None:
                    payments.record_result(impl.code, event.result)
        except ProviderError as exc:
            logger.error("webhook %s: %s failed: %s", provider, event.key, exc)
            return HttpResponse(status=500)
    return HttpResponse(status=200)


# Old provider-specific URLs, still configured in the Flutterwave and Stripe dashboards and in
# links from checkouts started before the switch.


def callback(request):
    return payment_return(request, "flutterwave")


@csrf_exempt
def webhook(request):
    return provider_webhook(request, "flutterwave")


def stripe_success(request):
    return payment_return(request, "stripe")


@csrf_exempt
def stripe_webhook(request):
    return provider_webhook(request, "stripe")


# --- Cancel / resume --------------------------------------------------------------


def _can_manage_billing(request, account) -> bool:
    from apps.accounts.models import Membership

    return Membership.objects.filter(
        user=request.user,
        account=account,
        role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
    ).exists()


@login_required
@require_POST
def cancel_subscription(request):
    """Cancel at the end of the paid period: nothing more is charged, and the business keeps
    what it paid for until then. A legacy provider-side subscription is stopped now."""
    account = get_current_account(request)
    if account is None:
        return redirect("/dashboard/")
    if not _can_manage_billing(request, account):
        messages.error(
            request, "Only an account owner or admin can cancel the subscription."
        )
        return _back_to_plans(request)

    sub = getattr(account, "subscription", None)
    if not sub or sub.status not in (Subscription.ACTIVE, Subscription.PAST_DUE):
        messages.error(request, "No active subscription to cancel.")
        return _back_to_plans(request)

    try:
        _stop_legacy_recurring(sub)
    except ProviderError as exc:
        logger.error("cancel_subscription: account=%s: %s", account.pk, exc)
        messages.error(request, f"Could not stop recurring billing: {exc}")
        return _back_to_plans(request)

    if sub.status == Subscription.PAST_DUE or not sub.current_period_end:
        sub.status = Subscription.CANCELLED
        sub.cancelled_at = timezone.now()
        sub.save(update_fields=["status", "cancelled_at", "updated_at"])
        messages.success(request, "Your subscription has been cancelled.")
    else:
        sub.cancel_at_period_end = True
        sub.save(update_fields=["cancel_at_period_end", "updated_at"])
        messages.success(
            request,
            f"Your subscription will end on {sub.current_period_end:%d %b %Y}. "
            "You won't be charged again.",
        )
    return _back_to_plans(request)


def _stop_legacy_recurring(sub) -> None:
    """Stop a provider-side recurring subscription from before Akilent ran renewals."""
    for code, field in (
        ("stripe", "stripe_subscription_id"),
        ("flutterwave", "fw_subscription_id"),
    ):
        if getattr(sub, field):
            impl = registry.get(code)
            if impl is not None:
                impl.cancel_legacy(sub)
            setattr(sub, field, None)
            sub.save(update_fields=[field, "updated_at"])


@login_required
@require_POST
def resume_subscription(request):
    """Undo a cancel before the period ends."""
    account = get_current_account(request)
    if account is None:
        return redirect("/dashboard/")
    if not _can_manage_billing(request, account):
        messages.error(
            request, "Only an account owner or admin can change the subscription."
        )
        return _back_to_plans(request)
    sub = getattr(account, "subscription", None)
    if sub and sub.cancel_at_period_end and sub.status == Subscription.ACTIVE:
        sub.cancel_at_period_end = False
        sub.save(update_fields=["cancel_at_period_end", "updated_at"])
        messages.success(request, "Your subscription will renew as normal.")
    return _back_to_plans(request)


def _back_to_plans(request):
    """Plain redirect for normal form posts. The cancel form is data-ajax, and
    fetch() follows a 302 silently without updating the page — so for XHR
    return the JSON {redirect} shape app.js turns into a real navigation. The
    queued flash message is then shown by that fresh page load."""
    if is_ajax(request):
        return ajax_redirect("/billing/plans/")
    return redirect("/billing/plans/")


# --- Manual (offline) payments -------------------------------------------------


@login_required
@require_POST
def manual_submit(request):
    """Tenant submits a reference/proof for an offline payment; goes into the
    admin review queue rather than activating anything immediately."""
    account = get_current_account(request)
    if account is None:
        return redirect("/dashboard/")

    if not SiteSettings.load().payments_enabled:
        messages.error(request, "Billing is currently disabled. Contact support.")
        return redirect("/billing/plans/")

    if not registry.manual_enabled():
        messages.error(request, "Bank transfer is not currently available.")
        return redirect("/billing/plans/")

    payment = get_object_or_404(
        BillingPayment,
        reference=request.POST.get("payment"),
        account=account,
        provider="manual",
        status=BillingPayment.PENDING,
    )
    reference = (request.POST.get("reference") or "").strip()
    if not reference:
        messages.error(request, "A payment reference is required.")
        return redirect(
            f"/billing/checkout/?plan={payment.plan.slug}&period={payment.billing_period}"
            "&method=manual"
        )

    req = ManualPaymentRequest.objects.create(
        account=account,
        plan=payment.plan,
        billing_period=payment.billing_period,
        payment=payment,
        reference=reference,
        proof=request.FILES.get("proof"),
    )
    from .tasks import notify_admins_of_manual_payment

    notify_admins_of_manual_payment.delay(req.pk)

    messages.success(request, "Payment submitted. An admin will review it shortly.")
    return redirect("/billing/plans/")


@admin_required
@require_POST
def manual_approve(request, pk):
    req = get_object_or_404(
        ManualPaymentRequest, pk=pk, status=ManualPaymentRequest.PENDING
    )
    if req.payment is not None:
        from .providers.manual import approved_result

        payments.record_result("manual", approved_result(req.payment))
    else:  # submitted before payments were recorded
        activate_subscription(
            req.account,
            req.plan,
            "manual",
            billing_period=req.billing_period or Subscription.MONTHLY,
        )
    req.status = ManualPaymentRequest.APPROVED
    req.reviewed_by = request.user
    req.reviewed_at = timezone.now()
    req.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    audit(
        request,
        "payment.approve",
        account=req.account,
        target=req.plan.name,
        reference=req.reference,
    )
    messages.success(request, f"Approved — {req.account} is now on {req.plan.name}.")
    return redirect("core:payments")


@admin_required
@require_POST
def manual_reject(request, pk):
    req = get_object_or_404(
        ManualPaymentRequest, pk=pk, status=ManualPaymentRequest.PENDING
    )
    req.status = ManualPaymentRequest.REJECTED
    req.note = (request.POST.get("note") or "").strip()
    req.reviewed_by = request.user
    req.reviewed_at = timezone.now()
    req.save(update_fields=["status", "note", "reviewed_by", "reviewed_at"])
    if req.payment is not None:
        payments.fail(req.payment, "Bank transfer rejected")
    audit(
        request,
        "payment.reject",
        account=req.account,
        target=req.plan.name,
        note=req.note,
    )
    messages.success(request, "Payment request rejected.")
    return redirect("core:payments")


@admin_required
@require_POST
def payment_method_toggle(request, pk):
    method = get_object_or_404(PaymentMethod, pk=pk)
    method.is_enabled = not method.is_enabled
    method.save(update_fields=["is_enabled"])
    audit(
        request,
        "payment_method.toggle",
        target=method.code,
        is_enabled=method.is_enabled,
    )
    messages.success(
        request, f"'{method.name}' {'enabled' if method.is_enabled else 'disabled'}."
    )
    return redirect("core:plans")


@admin_required
@require_POST
def payment_method_edit(request, pk):
    method = get_object_or_404(PaymentMethod, pk=pk)
    method.instructions = (request.POST.get("instructions") or "").strip()
    method.save(update_fields=["instructions"])
    audit(request, "payment_method.edit", target=method.code)
    messages.success(request, f"'{method.name}' instructions updated.")
    return redirect("core:plans")
