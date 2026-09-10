import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Count
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts import api as accounts_api
from apps.billing import api as billing_api
from apps.billing.models import Plan, Subscription, UsageSummary
from apps.core import docs as docs_kb
from apps.core import help as help_kb
from apps.core.forms import ConfigurationForm
from apps.core.models import Configurations, SiteSettings, MailProviderSettings
from apps.core.utils import admin_required

User = get_user_model()
logger = logging.getLogger(__name__)


# --- Customers ----------------------------------------------------------------

@admin_required
def customers(request):
    from apps.accounts.models import Account  # Admin view, direct import okay

    accounts = (
        Account.objects.all()
        .select_related("subscription", "subscription__plan")
        .annotate(member_count=Count("memberships", distinct=True))
        .order_by("company_name")
    )
    rows = [{
        "account": a,
        "owner": a.owner,
        "members": a.member_count,
        "subscription": billing_api.get_subscription(a),
        "emails_used": billing_api.get_email_usage(a),
    } for a in accounts]

    return render(request, "core/customers.html", {
        "rows": rows,
        "plans": Plan.objects.all().order_by("price_monthly"),
        "statuses": Subscription.STATUS_CHOICES,
    })


@admin_required
@require_POST
def customer_toggle(request, pk):
    from apps.accounts.models import Account  # Admin view, direct import okay
    a = get_object_or_404(Account, pk=pk)
    a.is_active = not a.is_active
    a.save(update_fields=["is_active"])
    messages.success(
        request, f"{a.company_name} {'activated' if a.is_active else 'deactivated'}."
    )
    return redirect("core:customers")


@admin_required
@require_POST
def customer_subscription(request, pk):
    from apps.accounts.models import Account  # Admin view, direct import okay
    a = get_object_or_404(Account, pk=pk)
    plan_id = request.POST.get("plan") or None
    plan = Plan.objects.filter(pk=plan_id).first() if plan_id else None
    status = request.POST.get("status")
    if plan is None or status not in dict(Subscription.STATUS_CHOICES):
        messages.error(request, "Pick a valid plan and status.")
        return redirect("core:customers")

    now = timezone.now()
    sub, created = Subscription.objects.get_or_create(
        account=a,
        defaults={"plan": plan, "status": status, "current_period_start": now},
    )
    if not created:
        sub.plan = plan
        sub.status = status
        if status == Subscription.CANCELLED:
            sub.cancelled_at = sub.cancelled_at or now
        elif status in (Subscription.ACTIVE, Subscription.TRIALING):
            sub.cancelled_at = None
            if not sub.current_period_end or sub.current_period_end < now:
                sub.current_period_end = now + timedelta(days=30)
        sub.save()
    messages.success(
        request, f"{a.company_name}: subscription set to {plan.name} ({status})."
    )
    return redirect("core:customers")


# --- Settings -----------------------------------------------------------------

@admin_required
def settings_page(request):
    site = SiteSettings.load()

    if request.method == "POST":
        site.app_name = (request.POST.get("app_name") or "Automator").strip() or "Automator"
        site.support_email = (request.POST.get("support_email") or "").strip()
        site.whatsapp_enabled = "whatsapp_enabled" in request.POST
        site.signups_enabled = "signups_enabled" in request.POST
        site.payments_enabled = "payments_enabled" in request.POST
        dp = request.POST.get("default_plan") or None
        site.default_plan = Plan.objects.filter(pk=dp).first() if dp else None
        try:
            site.default_trial_days = max(0, int(request.POST.get("default_trial_days") or 0))
        except ValueError:
            pass
        if request.FILES.get("logo"):
            site.logo = request.FILES["logo"]
        site.save()
        messages.success(request, "Settings saved.")
        return redirect("core:settings")

    return render(request, "core/settings.html", {
        "plans": Plan.objects.all().order_by("price_monthly"),
        "admins": User.objects.order_by("-is_superuser", "username"),
        "mail": MailProviderSettings.load(),
    })


@admin_required
@require_POST
def mail_settings_save(request):
    """Save mail provider settings (SES, SMTP, validation config)."""
    mail = MailProviderSettings.load()

    mail.infra_backend = request.POST.get("infra_backend") or mail.infra_backend
    mail.send_backend = request.POST.get("send_backend") or mail.send_backend
    mail.aws_region = (request.POST.get("aws_region") or "").strip() or mail.aws_region
    mail.ses_configuration_set = (request.POST.get("ses_configuration_set") or "").strip()
    mail.ses_sns_topic_arn = (request.POST.get("ses_sns_topic_arn") or "").strip()

    try:
        mail.ses_send_rate_limit = max(1, int(request.POST.get("ses_send_rate_limit") or mail.ses_send_rate_limit))
    except ValueError:
        pass

    mail.smtp_require_tls = "smtp_require_tls" in request.POST
    mail.enable_recipient_validation = "enable_recipient_validation" in request.POST

    try:
        mail.mx_validation_cache_ttl_seconds = max(0, int(request.POST.get("mx_validation_cache_ttl_seconds") or mail.mx_validation_cache_ttl_seconds))
    except ValueError:
        pass

    try:
        mail.soft_bounce_threshold = max(1, int(request.POST.get("soft_bounce_threshold") or mail.soft_bounce_threshold))
    except ValueError:
        pass

    mail.save()
    messages.success(request, "Mail settings saved.")
    return redirect("core:settings")


# --- Help center (public knowledge base) --------------------------------------

def help_index(request):
    q = (request.GET.get("q") or "").strip()
    results = help_kb.search(q) if q else None
    return render(request, "help/index.html", {
        "q": q,
        "results": results,
        "categories": help_kb.grouped(),
    })


def help_article(request, slug):
    article = help_kb.get_article(slug)
    if article is None:
        raise Http404("No such help article")
    return render(request, "help/article.html", {
        "article": article,
        "related": help_kb.related_to(article),
    })


# --- Developer docs (public) ---------------------------------------------------

def docs_page(request, slug="index"):
    from django.conf import settings

    page = docs_kb.get_page(slug)
    if page is None:
        raise Http404("No such docs page")
    prev_page, next_page = docs_kb.neighbors(page)
    return render(request, page.template, {
        "page": page,
        "pages": docs_kb.PAGES,
        "prev_page": prev_page,
        "next_page": next_page,
        "smtp_relay_host": settings.SMTP_RELAY_HOST,
        "smtp_relay_port": settings.SMTP_RELAY_PORT,
    })


@admin_required
@require_POST
def user_toggle_admin(request, pk):
    u = get_object_or_404(User, pk=pk)
    if u == request.user:
        messages.error(request, "You can't change your own admin status.")
        return redirect("core:settings")
    u.is_superuser = not u.is_superuser
    if u.is_superuser:
        u.is_staff = True
    u.save(update_fields=["is_superuser", "is_staff"])
    messages.success(
        request,
        f"{u.get_username()} is {'now a platform admin' if u.is_superuser else 'no longer an admin'}.",
    )
    return redirect("core:settings")


@admin_required
def create_configuration(request):
    if request.method == "POST":
        form = ConfigurationForm(request.POST)

        if form.is_valid():
            form.save()

            messages.success(
                request,
                "Configuration created successfully.",
            )

            return redirect("core:configurations-list")

    else:
        form = ConfigurationForm()

    return render(
        request,
        "core/create_configuration.html",
        {
            "form": form,
        },
    )


@admin_required
def configurations_list(request):
    configurations = Configurations.objects.all()

    return render(
        request,
        "core/configurations_list.html",
        {
            "configurations": configurations,
        },
    )


@admin_required
def edit_configuration(request, pk):
    configuration = get_object_or_404(Configurations, pk=pk)

    if request.method == "POST":
        form = ConfigurationForm(request.POST, instance=configuration)

        if form.is_valid():
            form.save()
            messages.success(request, "Configuration updated successfully.")
            return redirect("core:configurations-list")
        else:
            # 👇 Call your helper here so the red borders show up
            form.add_error_classes() 

    else:
        form = ConfigurationForm(instance=configuration)

    return render(
        request,
        "core/edit_configuration.html",
        {
            "form": form,
            "configuration": configuration,
        },
    )


@admin_required
def styleguide(request):
    """Living component gallery — the contract for every Akilent screen.

    Renders every design-system primitive so drift is visible and new screens
    have a reference. See docs/design/akilent-ui-spec.md §7.
    """
    return render(request, "core/styleguide.html")


@admin_required
def delete_configuration(request, pk):
    configuration = get_object_or_404(Configurations, pk=pk)

    if request.method == "POST":
        configuration.delete()

        messages.success(
            request,
            "Configuration deleted successfully.",
        )

        return redirect("core:configurations-list")

    return render(
        request,
        "core/delete_configuration.html",
        {
            "configuration": configuration,
        },
    )
