import json
import logging

import stripe
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings

from apps.accounts.utils import ajax_redirect, get_current_account, is_ajax
from apps.core.audit import audit
from apps.core.utils import admin_required
from apps.core.models import SiteSettings
from .models import ManualPaymentRequest, Plan, PaymentMethod, ProcessedWebhookEvent, Subscription, UsageSummary
from .flutterwave import FlutterwaveError, get_fw_client
from .gateways import enabled_payment_methods, get_gateway
from .pricing import DEFAULT_PERIOD_MULTIPLIERS, PERIOD_MONTH_MULTIPLES
from .services import activate_subscription

logger = logging.getLogger(__name__)


def _amount_covers_plan(amount, plan) -> bool:
    """True if a charged amount is at least the plan's monthly price."""
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(amount)) >= plan.price_monthly
    except (InvalidOperation, TypeError):
        return False


@login_required
def pricing_page(request):
    """The business's own plans page. Operators manage packages, payment methods and bank
    transfers in the Operator console (/manage/plans/, /manage/payments/), not here."""
    account = get_current_account(request)
    if account is None:
        return redirect("/dashboard/")

    plans = list(Plan.objects.filter(is_active=True).order_by("price_monthly"))
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

    return render(request, "billing/plans.html", {
        "plans": plans,
        "account": account,
        "subscription": subscription,
        "current_plan_slug": current_plan_slug,
        "plan_service_type_choices": Plan.SERVICE_TYPE_CHOICES,
        "conversations_used": UsageSummary.get_current_usage(account) if account else 0,
        "emails_used": UsageSummary.get_current_email_usage(account) if account else 0,
        "payments_enabled": site.payments_enabled,
        "payment_methods": enabled_payment_methods() if site.payments_enabled else [],
        "billing_periods": Subscription.BILLING_PERIOD_CHOICES,
        "period_pricing_json": json.dumps(period_pricing),
    })


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
        "max_conversations_per_month": _int("max_conversations_per_month"),
        "max_emails_per_month": _int("max_emails_per_month"),
        "max_automation_rules": _int("max_automation_rules"),
        "max_whatsapp_numbers": _int("max_whatsapp_numbers"),
        "max_bulk_recipients_per_campaign": _int("max_bulk_recipients_per_campaign", 500),
        "trial_days": _int("trial_days"),
        "log_retention_days": _int("log_retention_days"),
        "flutterwave_plan_id": (post.get("flutterwave_plan_id") or "").strip() or None,
        "has_priority_support": "has_priority_support" in post,
        "email_apis": "email_apis" in post,
        "inbound_email": "inbound_email" in post,
        "tracking_webhooks": "tracking_webhooks" in post,
        "detailed_analytics": "detailed_analytics" in post,
        "bulk_email": "bulk_email" in post,
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
    Plan.objects.create(slug=slug, **fields)
    audit(request, "plan.create", target=slug)
    messages.success(request, f"Package '{fields['name']}' created.")
    return redirect("core:plans")


@admin_required
@require_POST
def plan_edit(request, pk):
    plan = get_object_or_404(Plan, pk=pk)
    fields = _plan_form_fields(request.POST)
    if not fields["name"]:
        messages.error(request, "Package name is required.")
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
    plan.is_active = not plan.is_active
    plan.save(update_fields=["is_active"])
    audit(request, "plan.toggle", target=plan.slug, is_active=plan.is_active)
    messages.success(
        request, f"Package '{plan.name}' {'activated' if plan.is_active else 'deactivated'}."
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


@login_required
def checkout(request):
    account = get_current_account(request)
    if account is None:
        return redirect("/dashboard/")

    if not SiteSettings.load().payments_enabled:
        messages.error(request, "Billing is currently disabled. Contact support.")
        return redirect("/billing/plans/")

    plan_slug = request.GET.get("plan")
    plan = get_object_or_404(Plan, slug=plan_slug, is_active=True)
    if plan.slug == Plan.TRIAL:
        messages.error(request, "Trial plan cannot be purchased.")
        return redirect("/billing/plans/")

    method_code = request.GET.get("method")
    method = PaymentMethod.objects.filter(code=method_code, is_enabled=True).first() if method_code else None
    if method is None:
        method = enabled_payment_methods()[0] if enabled_payment_methods() else None
    gateway = get_gateway(method.code) if method else None
    if gateway is None:
        messages.error(request, "No payment method is currently available. Contact support.")
        return redirect("/billing/plans/")

    period = request.GET.get("period", Subscription.MONTHLY)
    if period not in dict(Subscription.BILLING_PERIOD_CHOICES):
        messages.error(request, "Invalid billing period selected.")
        return redirect("/billing/plans/")

    return gateway.start_checkout(request, account, plan)


def callback(request):
    status = request.GET.get("status")
    transaction_id = request.GET.get("transaction_id")

    if status != "successful":
        messages.error(request, "Payment was not completed successfully.")
        return redirect("/billing/plans/")

    account_id = request.session.pop("pending_account_id", None)
    plan_slug = request.session.pop("pending_plan_slug", None)
    request.session.pop("pending_tx_ref", None)

    if not account_id or not plan_slug:
        messages.error(request, "Session expired. Please try again.")
        return redirect("/billing/plans/")

    try:
        fw = get_fw_client()
        transaction = fw.verify_transaction(transaction_id)
    except FlutterwaveError as exc:
        logger.error("callback: verification failed: %s", exc)
        messages.error(request, "Payment verification failed. Contact support if charged.")
        return redirect("/billing/plans/")

    if transaction.get("status") != "successful":
        messages.error(request, "Payment could not be verified.")
        return redirect("/billing/plans/")

    try:
        from apps.accounts.models import Account
        account = Account.objects.get(pk=account_id)
        plan = Plan.objects.get(slug=plan_slug)
    except Exception as exc:
        logger.error("callback: account/plan lookup failed: %s", exc)
        messages.error(request, "Subscription activation failed. Contact support.")
        return redirect("/dashboard/")

    # Trust the verified transaction, not the redirect: confirm the amount
    # actually charged covers the plan we're about to grant.
    if not _amount_covers_plan(transaction.get("amount"), plan):
        logger.error(
            "callback: amount mismatch account=%s plan=%s charged=%s expected=%s",
            account.pk, plan.slug, transaction.get("amount"), plan.price_monthly,
        )
        messages.error(request, "Payment amount did not match the plan. Contact support if charged.")
        return redirect("/billing/plans/")

    cust_email = (transaction.get("customer") or {}).get("email")
    activate_subscription(
        account, plan, "flutterwave", billing_period=Subscription.MONTHLY, fw_customer_email=cust_email
    )

    # Capture the Flutterwave recurring-subscription id so it can be cancelled later.
    if plan.flutterwave_plan_id and cust_email:
        try:
            subs = get_fw_client().get_subscriptions(
                email=cust_email, plan_id=plan.flutterwave_plan_id
            )
            if subs:
                Subscription.objects.filter(account=account).update(
                    fw_subscription_id=str(subs[0].get("id") or "")
                )
        except FlutterwaveError as exc:
            logger.warning("callback: could not capture fw_subscription_id: %s", exc)

    logger.info(
        "callback: activated %s subscription for account=%s tx=%s",
        plan.name, account.pk, transaction_id,
    )
    messages.success(request, f"Successfully subscribed to {plan.name}!")
    return redirect("/dashboard/")


@login_required
@require_POST
def cancel_subscription(request):
    """Tenant cancels their own subscription — cancels the recurring charge on
    Flutterwave (if any), then marks it cancelled locally."""
    account = get_current_account(request)
    if account is None:
        return redirect("/dashboard/")

    # Only an owner/admin of the account may cancel its billing.
    from apps.accounts.models import Membership

    is_privileged = Membership.objects.filter(
        user=request.user,
        account=account,
        role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
    ).exists()
    if not is_privileged:
        messages.error(request, "Only an account owner or admin can cancel the subscription.")
        return _back_to_plans(request)

    sub = getattr(account, "subscription", None)
    if not sub or not sub.is_active:
        messages.error(request, "No active subscription to cancel.")
        return _back_to_plans(request)

    if sub.fw_subscription_id:
        try:
            get_fw_client().cancel_subscription(sub.fw_subscription_id)
        except FlutterwaveError as exc:
            logger.error("cancel_subscription: FW error for account=%s: %s", account.pk, exc)
            messages.error(request, f"Could not cancel recurring billing: {exc}")
            return _back_to_plans(request)
    elif sub.payment_method == "stripe" and sub.stripe_subscription_id:
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            stripe.Subscription.delete(sub.stripe_subscription_id)
        except stripe.error.StripeError as exc:
            logger.error("cancel_subscription: Stripe error for account=%s: %s", account.pk, exc)
            messages.error(request, f"Could not cancel recurring billing: {exc}")
            return _back_to_plans(request)

    sub.status = Subscription.CANCELLED
    sub.cancelled_at = timezone.now()
    sub.save(update_fields=["status", "cancelled_at", "updated_at"])
    messages.success(request, "Your subscription has been cancelled.")
    return _back_to_plans(request)


def _back_to_plans(request):
    """Plain redirect for normal form posts. The cancel form is data-ajax, and
    fetch() follows a 302 silently without updating the page — so for XHR
    return the JSON {redirect} shape app.js turns into a real navigation. The
    queued flash message is then shown by that fresh page load."""
    if is_ajax(request):
        return ajax_redirect("/billing/plans/")
    return redirect("/billing/plans/")


@admin_required
@require_POST
def plan_sync_fw(request, pk):
    """Admin: create the Flutterwave recurring payment plan for a package."""
    plan = get_object_or_404(Plan, pk=pk)
    if plan.price_monthly <= 0:
        messages.error(request, "Free/trial packages don't need a Flutterwave plan.")
        return redirect("core:plans")
    currency = getattr(settings, "FLUTTERWAVE_CURRENCY", "USD")
    try:
        fp = get_fw_client().create_payment_plan(
            name=plan.name, amount=plan.price_monthly, interval="monthly", currency=currency
        )
        plan.flutterwave_plan_id = str(fp.get("id") or "")
        plan.save(update_fields=["flutterwave_plan_id"])
        audit(request, "plan.sync_flutterwave", target=plan.slug)
        messages.success(
            request, f"Recurring plan created on Flutterwave (id {plan.flutterwave_plan_id})."
        )
    except FlutterwaveError as exc:
        messages.error(request, f"Flutterwave error: {exc}")
    return redirect("core:plans")


@csrf_exempt
@require_POST
def webhook(request):
    verif_hash = request.headers.get("verif-hash")
    expected = getattr(settings, "FLUTTERWAVE_WEBHOOK_HASH", None)

    if not expected or verif_hash != expected:
        logger.warning("webhook: invalid verif-hash")
        return HttpResponse(status=401)

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    event = payload.get("event")
    data = payload.get("data", {}) or {}

    # Idempotency: ignore replays of an event we've already processed. The key is
    # the event type plus the provider object id (transaction/subscription id).
    object_id = data.get("id")
    if object_id is not None:
        event_key = f"{event}:{object_id}"
        _, created = ProcessedWebhookEvent.objects.get_or_create(event_key=event_key)
        if not created:
            logger.info("webhook: duplicate event ignored key=%s", event_key)
            return HttpResponse(status=200)

    if event == "charge.completed" and (data.get("meta") or {}).get("order_id"):
        _handle_commerce_charge_completed(payload)
    elif event == "charge.completed":
        _handle_charge_completed(payload)
    elif event == "subscription.cancelled":
        _handle_subscription_cancelled(payload)
    else:
        logger.debug("webhook: unhandled event type=%s", event)

    return HttpResponse(status=200)


def _handle_charge_completed(payload: dict):
    data = payload.get("data", {})
    if data.get("status") != "successful":
        return

    meta = data.get("meta", {}) or {}
    account_id = meta.get("account_id")
    plan_slug = meta.get("plan_slug")

    if not account_id:
        logger.warning("_handle_charge_completed: no account_id in meta")
        return

    try:
        sub = Subscription.objects.select_related("plan").get(
            account_id=account_id
        )
    except Subscription.DoesNotExist:
        logger.warning("_handle_charge_completed: no subscription for account=%s", account_id)
        return

    # Resolve the plan we're being asked to grant (fall back to the current one).
    plan = sub.plan
    if plan_slug:
        plan = Plan.objects.filter(slug=plan_slug).first() or plan

    # Don't trust the webhook body alone — independently verify the transaction
    # with Flutterwave and confirm the charged amount covers the plan.
    transaction_id = data.get("id")
    try:
        verified = get_fw_client().verify_transaction(transaction_id)
    except FlutterwaveError as exc:
        logger.error("_handle_charge_completed: verify failed tx=%s: %s", transaction_id, exc)
        return

    if verified.get("status") != "successful" or not _amount_covers_plan(verified.get("amount"), plan):
        logger.error(
            "_handle_charge_completed: rejected account=%s plan=%s status=%s amount=%s expected=%s",
            account_id, plan.slug, verified.get("status"), verified.get("amount"), plan.price_monthly,
        )
        return

    cust_email = (verified.get("customer") or {}).get("email") or sub.fw_customer_email
    activate_subscription(
        sub.account, plan, "flutterwave", billing_period=Subscription.MONTHLY, fw_customer_email=cust_email
    )

    logger.info("_handle_charge_completed: renewed subscription for account=%s", account_id)


def _handle_commerce_charge_completed(payload: dict):
    """Route a Flutterwave charge tagged with an ``order_id`` to apps.commerce.

    Shares this endpoint, its ``verif-hash`` check, and its
    ``ProcessedWebhookEvent`` idempotency ledger with the subscription-billing
    handler above rather than standing up a second webhook — the two are
    distinguished purely by ``meta.order_id`` being present.
    """
    from apps.commerce.models import Payment
    from apps.commerce.services import mark_failed, mark_paid

    data = payload.get("data", {})
    meta = data.get("meta") or {}
    payment_id = meta.get("payment_id")
    order_id = meta.get("order_id")

    payment = Payment.objects.filter(public_id=payment_id, order__public_id=order_id).first()
    if payment is None:
        logger.warning(
            "_handle_commerce_charge_completed: no payment for payment_id=%s order_id=%s",
            payment_id, order_id,
        )
        return

    transaction_id = data.get("id")
    try:
        verified = get_fw_client().verify_transaction(transaction_id)
    except FlutterwaveError as exc:
        logger.error("_handle_commerce_charge_completed: verify failed tx=%s: %s", transaction_id, exc)
        return

    from decimal import Decimal, InvalidOperation

    try:
        covers = Decimal(str(verified.get("amount"))) >= payment.amount
    except (InvalidOperation, TypeError):
        covers = False

    if verified.get("status") != "successful" or not covers:
        logger.error(
            "_handle_commerce_charge_completed: rejected payment=%s status=%s amount=%s expected=%s",
            payment.public_id, verified.get("status"), verified.get("amount"), payment.amount,
        )
        mark_failed(payment, error=f"verification rejected: status={verified.get('status')}")
        return

    mark_paid(payment, transaction_id=str(transaction_id), raw_payload=verified)
    logger.info("_handle_commerce_charge_completed: order %s paid", order_id)


def _handle_subscription_cancelled(payload: dict):
    data = payload.get("data", {})
    meta = (data.get("meta") or {})
    account_id = meta.get("account_id")

    if not account_id:
        return

    now = timezone.now()
    updated = Subscription.objects.filter(account_id=account_id).update(
        status=Subscription.CANCELLED,
        cancelled_at=now,
    )
    if updated:
        logger.info("_handle_subscription_cancelled: cancelled subscription for account=%s", account_id)


# --- Stripe -----------------------------------------------------------------

@login_required
def stripe_success(request):
    """Stripe Checkout's success_url landing page.

    The redirect alone isn't proof of payment, so this independently
    retrieves and verifies the Checkout Session (same pattern as
    Flutterwave's callback()) and activates immediately rather than waiting
    on the webhook — the webhook remains the authoritative idempotent
    backstop for this and for renewals.

    A bare session_id in the URL is not sufficient authorization to activate
    a subscription: login is required, and activation is scoped to the
    caller's own current account. A session that verifies but belongs to a
    different account is rejected silently (same response as "not paid
    yet") so we never reveal to the browser that the session belongs to
    someone else.
    """
    account = get_current_account(request)
    session_id = request.GET.get("session_id")
    sub = None
    if account and session_id:
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            session = _stripe_to_dict(stripe.checkout.Session.retrieve(session_id))
            sub = _activate_stripe_session(session, account=account)
        except stripe.error.StripeError as exc:
            logger.warning("stripe_success: could not retrieve session=%s: %s", session_id, exc)

    if sub is not None:
        messages.success(request, f"Payment confirmed — you're on {sub.plan.name}.")
        from apps.accounts import onboarding as ob

        if account.onboarding_state != account.Onboarding.COMPLETED:
            return redirect(ob.first_setup_url(account))
        return redirect("/dashboard/")

    return render(request, "billing/stripe_pending.html")


@csrf_exempt
@require_POST
def stripe_webhook(request):
    stripe.api_key = settings.STRIPE_SECRET_KEY
    sig_header = request.headers.get("Stripe-Signature")
    webhook_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", None)

    if not webhook_secret:
        logger.warning("stripe_webhook: STRIPE_WEBHOOK_SECRET not configured")
        return HttpResponse(status=401)

    try:
        event = stripe.Webhook.construct_event(request.body, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        logger.warning("stripe_webhook: invalid signature")
        return HttpResponse(status=400)

    event_key = f"{event['type']}:{event['id']}"
    data = _stripe_to_dict(event["data"]["object"])

    # The dedupe row and the handler share one transaction: if the handler
    # raises, the row rolls back, so Stripe's retry is processed rather than
    # ignored as an already-seen event.
    with transaction.atomic():
        _, created = ProcessedWebhookEvent.objects.get_or_create(event_key=event_key)
        if not created:
            logger.info("stripe_webhook: duplicate event ignored key=%s", event_key)
            return HttpResponse(status=200)

        if event["type"] == "checkout.session.completed":
            _handle_stripe_checkout_completed(data)
        elif event["type"] == "invoice.payment_succeeded":
            _handle_stripe_invoice_paid(data)
        elif event["type"] == "invoice.payment_failed":
            _handle_stripe_payment_failed(data)
        elif event["type"] == "customer.subscription.deleted":
            _handle_stripe_subscription_deleted(data)
        else:
            logger.debug("stripe_webhook: unhandled event type=%s", event["type"])

    return HttpResponse(status=200)


def _stripe_to_dict(obj) -> dict:
    """Stripe SDK objects (v15+) are not dicts — no .get(). Convert at the
    boundary so the handlers can treat payloads as plain dicts."""
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


def _verify_stripe_session(session: dict) -> dict | None:
    """Stripe-level requirements that must hold before ANY caller is allowed
    to activate from this session — independent of who's asking, and treating
    the session's metadata as untrusted data to validate, not business state
    to trust outright."""
    if session.get("mode") != "subscription":
        return None
    if session.get("payment_status") != "paid":
        return None
    if not session.get("subscription"):  # the Stripe subscription object id
        return None
    meta = session.get("metadata") or {}
    if not meta.get("account_id") or not meta.get("plan_slug"):
        return None
    return session


def _activate_stripe_session(session: dict, *, account=None):
    """Activate a Subscription from a verified Stripe Checkout Session.

    ``account`` is the caller's authorization context:
    - Webhook caller (server-to-server, Stripe-signature-verified): omit —
      trusted to activate whichever account the session's metadata names.
    - Browser caller (stripe_success): MUST pass the requesting user's
      current account. If the session's metadata.account_id doesn't match,
      the request is rejected — otherwise anyone who learns/guesses a
      session_id could activate billing for an account that isn't theirs.
    """
    session = _verify_stripe_session(session)
    if session is None:
        return None

    meta = session["metadata"]
    if account is not None and str(account.pk) != str(meta["account_id"]):
        # Log the mismatch for our own audit trail, but the caller (stripe_success)
        # must respond identically to this and to "not paid yet" — never reveal
        # to the browser that the session belongs to a different account.
        logger.warning(
            "_activate_stripe_session: session account_id=%s does not match caller account=%s",
            meta["account_id"], account.pk,
        )
        return None

    from apps.accounts.models import Account

    try:
        target_account = account or Account.objects.get(pk=meta["account_id"])
        # is_active=True: a plan deactivated between Checkout creation and
        # completion must not still be grantable.
        plan = Plan.objects.get(slug=meta["plan_slug"], is_active=True)
    except (Account.DoesNotExist, Plan.DoesNotExist) as exc:
        logger.error("_activate_stripe_session: account/plan lookup failed: %s", exc)
        return None

    # A Subscription row must already exist for this account — activation
    # only ever rolls an existing row forward (update_or_create inside
    # activate_subscription), it never conjures one from nothing. Any prior
    # status is a legitimate starting point (INCOMPLETE from a fresh paid
    # signup, PAST_DUE recovering via a new Checkout, TRIALING upgrading to
    # a paid plan, or a replayed ACTIVE session) — the account-id match
    # above is what actually prevents cross-account activation.
    existing_subscription = getattr(target_account, "subscription", None)
    if existing_subscription is None:
        logger.error("_activate_stripe_session: no subscription row for account=%s", target_account.pk)
        return None

    # If this subscription already has a Stripe customer on file (e.g. a
    # retry/second Checkout, or the webhook already ran), the session's
    # customer must match — guards against a stale or cross-account session
    # id being replayed here.
    existing_customer_id = existing_subscription.stripe_customer_id
    session_customer_id = session.get("customer")
    if existing_customer_id and session_customer_id and str(existing_customer_id) != str(session_customer_id):
        logger.warning(
            "_activate_stripe_session: session customer=%s does not match existing stripe_customer_id=%s",
            session_customer_id, existing_customer_id,
        )
        return None

    sub = activate_subscription(
        target_account,
        plan,
        "stripe",
        billing_period=meta.get("billing_period", Subscription.MONTHLY),
        stripe_customer_id=session_customer_id,
        stripe_subscription_id=session["subscription"],
    )
    logger.info("_activate_stripe_session: activated subscription for account=%s", target_account.pk)
    return sub


def _handle_stripe_checkout_completed(session: dict):
    _activate_stripe_session(session)


def _handle_stripe_invoice_paid(invoice: dict):
    stripe_subscription_id = invoice.get("subscription")
    if not stripe_subscription_id:
        return

    sub = Subscription.objects.select_related("plan", "account").filter(
        stripe_subscription_id=stripe_subscription_id
    ).first()
    if sub is None:
        logger.warning(
            "_handle_stripe_invoice_paid: no subscription for stripe_subscription_id=%s",
            stripe_subscription_id,
        )
        return

    # The first invoice of a new subscription (billing_reason=subscription_create)
    # is already handled by checkout.session.completed — only renewals
    # (subscription_cycle) should roll the period forward here.
    if invoice.get("billing_reason") != "subscription_cycle":
        return

    activate_subscription(
        sub.account,
        sub.plan,
        "stripe",
        billing_period=sub.billing_period,
        stripe_customer_id=sub.stripe_customer_id,
        stripe_subscription_id=sub.stripe_subscription_id,
    )
    logger.info("_handle_stripe_invoice_paid: renewed subscription for account=%s", sub.account_id)


def _handle_stripe_payment_failed(invoice: dict):
    stripe_subscription_id = invoice.get("subscription")
    if not stripe_subscription_id:
        return
    updated = Subscription.objects.filter(stripe_subscription_id=stripe_subscription_id).update(
        status=Subscription.PAST_DUE
    )
    if updated:
        logger.info(
            "_handle_stripe_payment_failed: marked past_due for stripe_subscription_id=%s",
            stripe_subscription_id,
        )


def _handle_stripe_subscription_deleted(subscription: dict):
    stripe_subscription_id = subscription.get("id")
    if not stripe_subscription_id:
        return
    updated = Subscription.objects.filter(stripe_subscription_id=stripe_subscription_id).update(
        status=Subscription.CANCELLED, cancelled_at=timezone.now()
    )
    if updated:
        logger.info(
            "_handle_stripe_subscription_deleted: cancelled stripe_subscription_id=%s",
            stripe_subscription_id,
        )


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

    method = PaymentMethod.objects.filter(code="manual", is_enabled=True).first()
    if method is None:
        messages.error(request, "Bank transfer is not currently available.")
        return redirect("/billing/plans/")

    plan = get_object_or_404(Plan, slug=request.POST.get("plan"), is_active=True)
    reference = (request.POST.get("reference") or "").strip()
    if not reference:
        messages.error(request, "A payment reference is required.")
        return redirect(f"/billing/checkout/?plan={plan.slug}&method=manual")

    req = ManualPaymentRequest.objects.create(
        account=account,
        plan=plan,
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
    req = get_object_or_404(ManualPaymentRequest, pk=pk, status=ManualPaymentRequest.PENDING)
    activate_subscription(req.account, req.plan, "manual")
    req.status = ManualPaymentRequest.APPROVED
    req.reviewed_by = request.user
    req.reviewed_at = timezone.now()
    req.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    audit(request, "payment.approve", account=req.account, target=req.plan.name, reference=req.reference)
    messages.success(request, f"Approved — {req.account} is now on {req.plan.name}.")
    return redirect("core:payments")


@admin_required
@require_POST
def manual_reject(request, pk):
    req = get_object_or_404(ManualPaymentRequest, pk=pk, status=ManualPaymentRequest.PENDING)
    req.status = ManualPaymentRequest.REJECTED
    req.note = (request.POST.get("note") or "").strip()
    req.reviewed_by = request.user
    req.reviewed_at = timezone.now()
    req.save(update_fields=["status", "note", "reviewed_by", "reviewed_at"])
    audit(request, "payment.reject", account=req.account, target=req.plan.name, note=req.note)
    messages.success(request, "Payment request rejected.")
    return redirect("core:payments")


@admin_required
@require_POST
def payment_method_toggle(request, pk):
    method = get_object_or_404(PaymentMethod, pk=pk)
    method.is_enabled = not method.is_enabled
    method.save(update_fields=["is_enabled"])
    audit(request, "payment_method.toggle", target=method.code, is_enabled=method.is_enabled)
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
