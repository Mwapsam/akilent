"""Operator Console: Akilent's own product-marketing email (apps.platform_mail).

Campaigns, lists (static/CSV and dynamic), subscribers and sender settings. Sending goes
through the system mail provider, so none of this depends on the tenant Email channel.
"""

from __future__ import annotations

from datetime import datetime

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.audit import audit
from apps.core.utils import admin_required
from apps.platform_mail.api import (
    MarketingCampaign,
    MarketingList,
    MarketingListMember,
    MarketingSettings,
    MarketingSubscriber,
    consent,
    lists,
    sending,
)
from apps.platform_mail.api import render as render_service

MAX_IMPORT_BYTES = 10 * 1024 * 1024


# --- Forms --------------------------------------------------------------------


class CampaignForm(forms.ModelForm):
    lists = forms.ModelMultipleChoiceField(
        queryset=MarketingList.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = MarketingCampaign
        fields = (
            "name",
            "subject",
            "preheader",
            "heading",
            "body",
            "cta_label",
            "cta_url",
            "lists",
        )
        widgets = {"body": forms.Textarea(attrs={"rows": 12})}


class ListForm(forms.ModelForm):
    ROLE_CHOICES = [
        ("all", "Everyone in a workspace"),
        ("owners", "Workspace owners only"),
    ]

    role = forms.ChoiceField(choices=ROLE_CHOICES, required=False)
    plans = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple
    )
    statuses = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple
    )
    joined_after = forms.DateField(required=False)
    joined_before = forms.DateField(required=False)

    class Meta:
        model = MarketingList
        fields = ("name", "description", "kind")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.billing.models import Plan, Subscription

        plans = self.fields["plans"]
        statuses = self.fields["statuses"]
        assert isinstance(plans, forms.MultipleChoiceField)
        assert isinstance(statuses, forms.MultipleChoiceField)
        plans.choices = list(
            Plan.objects.order_by("price_monthly").values_list("slug", "name")
        )
        statuses.choices = list(Subscription.STATUS_CHOICES)
        if self.instance.pk and self.instance.filters:
            f = self.instance.filters
            self.initial.update(
                role=f.get("role", "all"),
                plans=f.get("plans", []),
                statuses=f.get("statuses", []),
                joined_after=f.get("joined_after"),
                joined_before=f.get("joined_before"),
            )

    def save(self, commit=True):
        obj = super().save(commit=False)
        if obj.kind == MarketingList.Kind.DYNAMIC:
            cd = self.cleaned_data
            obj.filters = {
                "role": cd.get("role") or "all",
                "plans": cd.get("plans") or [],
                "statuses": cd.get("statuses") or [],
                "joined_after": cd["joined_after"].isoformat()
                if cd.get("joined_after")
                else "",
                "joined_before": cd["joined_before"].isoformat()
                if cd.get("joined_before")
                else "",
            }
        else:
            obj.filters = {}
        if commit:
            obj.save()
        return obj


class SettingsForm(forms.ModelForm):
    class Meta:
        model = MarketingSettings
        fields = ("postal_address", "from_name")
        widgets = {"postal_address": forms.Textarea(attrs={"rows": 3})}


class SubscriberForm(forms.Form):
    email = forms.EmailField()
    name = forms.CharField(max_length=200, required=False)
    consent_note = forms.CharField(
        max_length=255,
        label="How did they agree?",
        help_text="e.g. Asked at the Nairobi meetup, 3 Oct",
    )


# --- Campaigns ----------------------------------------------------------------


@admin_required
def campaigns(request):
    return render(
        request,
        "manage/marketing/campaigns.html",
        {
            "tab": "campaigns",
            "campaigns": MarketingCampaign.objects.prefetch_related("lists")[:200],
            "subscribed": MarketingSubscriber.objects.filter(
                status=MarketingSubscriber.Status.SUBSCRIBED
            ).count(),
        },
    )


@admin_required
def campaign_new(request):
    form = CampaignForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        campaign = form.save(commit=False)
        campaign.created_by = request.user
        campaign.save()
        form.save_m2m()
        audit(request, "marketing.campaign_create", target=campaign.name)
        return redirect("core:marketing-campaign", pk=campaign.pk)
    return render(
        request,
        "manage/marketing/campaign_form.html",
        {"tab": "campaigns", "form": form},
    )


@admin_required
def campaign_detail(request, pk):
    campaign = get_object_or_404(MarketingCampaign, pk=pk)
    form = None
    if campaign.is_editable:
        form = CampaignForm(request.POST or None, instance=campaign)
        if request.method == "POST":
            if form.is_valid():
                form.save()
                messages.success(request, "Saved.")
                return redirect("core:marketing-campaign", pk=campaign.pk)
    ctx = {
        "tab": "campaigns",
        "campaign": campaign,
        "form": form,
        "problems": sending.problems(campaign),
        "recipients": sending.recipient_count(campaign),
        "stats": sending.stats(campaign) if campaign.started_at else None,
        "failures": campaign.sends.filter(status="failed").select_related("subscriber")[
            :50
        ],
    }
    return render(request, "manage/marketing/campaign_detail.html", ctx)


@admin_required
def campaign_preview(request, pk):
    campaign = get_object_or_404(MarketingCampaign, pk=pk)
    _, _, html = render_service.render(campaign, token=render_service.PREVIEW_TOKEN)
    response = HttpResponse(html)
    response["X-Frame-Options"] = "SAMEORIGIN"
    # Preview HTML never runs script on the console origin; links still open in a new tab.
    response["Content-Security-Policy"] = (
        "sandbox allow-popups allow-popups-to-escape-sandbox"
    )
    return response


@admin_required
@require_POST
def campaign_action(request, pk, action):
    campaign = get_object_or_404(MarketingCampaign, pk=pk)
    try:
        if action == "test":
            to = (request.POST.get("to") or request.user.email or "").strip()
            if not to:
                raise sending.CampaignError("Give an address to send the test to.")
            sending.send_test(campaign, to)
            messages.success(request, f"Test sent to {to}.")
        elif action == "send":
            n = sending.start(campaign)
            audit(
                request, "marketing.campaign_send", target=campaign.name, recipients=n
            )
            messages.success(request, f"Sending to {n:,} subscribers.")
        elif action == "schedule":
            when = _parse_local(request.POST.get("when", ""))
            sending.schedule(campaign, when)
            audit(
                request,
                "marketing.campaign_schedule",
                target=campaign.name,
                when=when.isoformat(),
            )
            messages.success(
                request, f"Scheduled for {timezone.localtime(when):%d %b %Y, %H:%M}."
            )
        elif action == "unschedule":
            sending.unschedule(campaign)
            messages.success(request, "Back to draft.")
        elif action == "cancel":
            dropped = sending.cancel(campaign)
            audit(
                request,
                "marketing.campaign_cancel",
                target=campaign.name,
                dropped=dropped,
            )
            messages.success(request, f"Cancelled. {dropped:,} unsent emails dropped.")
        elif action == "duplicate":
            copy = _duplicate(campaign, request.user)
            return redirect("core:marketing-campaign", pk=copy.pk)
        elif action == "delete" and campaign.status == MarketingCampaign.Status.DRAFT:
            campaign.delete()
            messages.success(request, "Draft deleted.")
            return redirect("core:marketing")
        else:
            raise Http404
    except sending.CampaignError as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # the provider refused a test send
        if action != "test":
            raise
        messages.error(request, f"The test email couldn't be sent: {exc}")
    return redirect("core:marketing-campaign", pk=campaign.pk)


def _parse_local(raw: str):
    try:
        naive = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise sending.CampaignError("Pick a date and time.") from exc
    return timezone.make_aware(naive) if timezone.is_naive(naive) else naive


def _duplicate(campaign, user):
    copy = MarketingCampaign.objects.create(
        name=f"{campaign.name} (copy)"[:150],
        subject=campaign.subject,
        preheader=campaign.preheader,
        heading=campaign.heading,
        body=campaign.body,
        cta_label=campaign.cta_label,
        cta_url=campaign.cta_url,
        created_by=user,
    )
    copy.lists.set(campaign.lists.all())
    return copy


# --- Lists --------------------------------------------------------------------


@admin_required
def list_index(request):
    form = ListForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        mlist = form.save(commit=False)
        mlist.created_by = request.user
        mlist.save()
        audit(request, "marketing.list_create", target=mlist.name)
        return redirect("core:marketing-list", pk=mlist.pk)
    rows = [
        (m, lists.members(m).filter(status="subscribed").count())
        for m in MarketingList.objects.all()
    ]
    return render(
        request,
        "manage/marketing/lists.html",
        {"tab": "lists", "rows": rows, "form": form},
    )


@admin_required
def list_detail(request, pk):
    mlist = get_object_or_404(MarketingList, pk=pk)
    form = ListForm(request.POST or None, instance=mlist)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "List saved.")
        return redirect("core:marketing-list", pk=mlist.pk)
    qs = lists.search(lists.members(mlist), request.GET.get("q", "")).order_by("email")
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "manage/marketing/list_detail.html",
        {
            "tab": "lists",
            "mlist": mlist,
            "form": form,
            "page": page,
            "q": request.GET.get("q", ""),
            "subscribed": lists.members(mlist).filter(status="subscribed").count(),
            "add_form": SubscriberForm(),
            "report": request.session.pop("marketing_import_report", None),
        },
    )


@admin_required
@require_POST
def list_import(request, pk):
    mlist = get_object_or_404(MarketingList, pk=pk, kind=MarketingList.Kind.STATIC)
    upload = request.FILES.get("file")
    note = (request.POST.get("consent_note") or "").strip()
    if not request.POST.get("confirm_consent") or not note:
        messages.error(
            request, "Confirm these people agreed to hear from Akilent, and say how."
        )
    elif upload is None:
        messages.error(request, "Choose a CSV file.")
    elif upload.size > MAX_IMPORT_BYTES:
        messages.error(
            request, "That file is over 10 MB. Split it and import the parts."
        )
    else:
        try:
            report = lists.import_csv(
                mlist, upload.read(), consent_note=note, actor=request.user
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            audit(
                request,
                "marketing.list_import",
                target=mlist.name,
                added=report.added,
                existing=report.existing,
                kept_opted_out=report.kept_opted_out,
                invalid=len(report.invalid),
            )
            request.session["marketing_import_report"] = {
                "added": report.added,
                "existing": report.existing,
                "kept_opted_out": report.kept_opted_out,
                "invalid": report.invalid[:100],
                "invalid_count": len(report.invalid),
            }
    return redirect("core:marketing-list", pk=mlist.pk)


@admin_required
@require_POST
def list_add_member(request, pk):
    mlist = get_object_or_404(MarketingList, pk=pk, kind=MarketingList.Kind.STATIC)
    form = SubscriberForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give a valid email and say how they agreed.")
        return redirect("core:marketing-list", pk=mlist.pk)
    cd = form.cleaned_data
    subscriber, outcome = consent.subscribe(
        cd["email"],
        source=MarketingSubscriber.Source.ADMIN,
        name=cd["name"],
        evidence={"operator_note": cd["consent_note"], "added_by": request.user.email},
    )
    if outcome == "kept_opted_out":
        messages.error(
            request, f"{subscriber.email} has opted out, so they can't be added back."
        )
    else:
        lists.add_member(mlist, subscriber)
        messages.success(request, f"Added {subscriber.email}.")
    return redirect("core:marketing-list", pk=mlist.pk)


@admin_required
@require_POST
def list_remove_member(request, pk, subscriber_pk):
    MarketingListMember.objects.filter(list_id=pk, subscriber_id=subscriber_pk).delete()
    messages.success(request, "Removed from the list. Their subscription is unchanged.")
    return redirect("core:marketing-list", pk=pk)


@admin_required
@require_POST
def list_delete(request, pk):
    mlist = get_object_or_404(MarketingList, pk=pk)
    audit(request, "marketing.list_delete", target=mlist.name)
    mlist.delete()
    messages.success(request, "List deleted. Subscribers on it are unchanged.")
    return redirect("core:marketing-lists")


# --- Subscribers and settings -------------------------------------------------


@admin_required
def subscribers(request):
    qs = MarketingSubscriber.objects.all()
    status = request.GET.get("status", "")
    if status in MarketingSubscriber.Status.values:
        qs = qs.filter(status=status)
    qs = lists.search(qs, request.GET.get("q", ""))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    counts = {
        s: MarketingSubscriber.objects.filter(status=s).count()
        for s in MarketingSubscriber.Status.values
    }
    return render(
        request,
        "manage/marketing/subscribers.html",
        {
            "tab": "subscribers",
            "page": page,
            "q": request.GET.get("q", ""),
            "status": status,
            "statuses": MarketingSubscriber.Status.choices,
            "counts": counts,
        },
    )


@admin_required
@require_POST
def subscriber_unsubscribe(request, pk):
    row = get_object_or_404(MarketingSubscriber, pk=pk)
    if consent.unsubscribe(row, how=f"operator:{request.user.email}"):
        audit(request, "marketing.subscriber_unsubscribe", target=row.email)
        messages.success(request, f"{row.email} won't get product updates any more.")
    back = request.POST.get("next", "")
    # Only back to a console page, never an outside address.
    return redirect(
        back if back.startswith("/manage/marketing/") else "core:marketing-subscribers"
    )


@admin_required
def marketing_settings(request):
    row = MarketingSettings.load()
    form = SettingsForm(request.POST or None, instance=row)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(request, "marketing.settings")
        messages.success(request, "Marketing settings saved.")
        return redirect("core:marketing-settings")
    from django.conf import settings as dj

    return render(
        request,
        "manage/marketing/settings.html",
        {
            "tab": "settings",
            "form": form,
            "from_email": getattr(dj, "PLATFORM_MARKETING_FROM_EMAIL", "")
            or dj.DEFAULT_FROM_EMAIL,
            "rate": getattr(dj, "PLATFORM_MARKETING_RATE_PER_MINUTE", 120),
        },
    )
