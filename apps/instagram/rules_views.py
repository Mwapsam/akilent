"""Business-facing comment rules: automatic private replies and comment moderation.

Until these pages existed the rules could only be set up in Django admin, so a
business (or Meta's App Reviewer) had no way to use comment features at all.

The rules are shared by Instagram and Facebook Page comments: each rule says which
channels it applies to. The pages are open to a business with either channel's
feature (``instagram`` or ``facebook``).
"""

from __future__ import annotations

from functools import wraps
from typing import cast

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.instagram.models import CommentTrigger, ModerationLog, ModerationRule
from apps.instagram.models.moderation import RuleChannels
from apps.instagram.services.default_rules import (
    add_default_moderation_rules,
    missing_default_rules,
)


def rule_channels(account) -> list[str]:
    """The comment channels this business can write rules for ("instagram", "facebook")."""
    from apps.billing import api as billing_api

    return [c for c in ("instagram", "facebook") if billing_api.usable(account, c)]


def comment_rules_required(view):
    """Open to a business that can use Instagram or the Facebook inbox."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        account = get_current_account(request)
        if account is not None and not rule_channels(account):
            return redirect(reverse("billing:locked", args=["instagram"]))
        return view(request, *args, **kwargs)

    return wrapper


class _ChannelsMixin(forms.ModelForm):
    """Shows "Applies to" only when the business has both channels; otherwise a rule
    applies to its one channel."""

    _only_channel = ""

    def setup_channels(self, channels: list[str]):
        # Keywords as chips (components/_keyword_chips.html); still one comma-separated field.
        if "keywords" in self.fields:
            self.fields["keywords"].chips = True  # type: ignore[attr-defined]
        self._only_channel = channels[0] if len(channels) == 1 else ""
        if self._only_channel:
            self.fields.pop("channels", None)
        else:
            self.fields["channels"].label = "Applies to comments on"

    def save(self, commit=True):
        rule = super().save(commit=False)
        if self._only_channel and not (rule.pk and rule.channels == RuleChannels.BOTH):
            rule.channels = self._only_channel
        if commit:
            rule.save()
        return rule


class CommentTriggerForm(_ChannelsMixin):
    class Meta:
        model = CommentTrigger
        fields = [
            "name",
            "channels",
            "match_type",
            "keywords",
            "reply_type",
            "reply_template",
        ]
        labels = {
            "match_type": "Reply when a comment…",
            "keywords": "Keywords",
            "reply_type": "Reply via",
            "reply_template": "Reply text",
        }
        help_texts = {
            "keywords": "For example price, how much, cost. Used with 'Keyword / phrase match'.",
            "reply_template": "{username} becomes their Instagram name, or their first name on Facebook. Sent once per comment thread.",
        }
        widgets = {
            "keywords": forms.TextInput(),
            "reply_template": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        data = super().clean() or {}
        if data.get("match_type") == CommentTrigger.MatchType.KEYWORD and not (
            data.get("keywords") or ""
        ).strip(" ,"):
            self.add_error("keywords", "Add at least one keyword.")
        return data


class ModerationRuleForm(_ChannelsMixin):
    class Meta:
        model = ModerationRule
        fields = [
            "name",
            "channels",
            "match_type",
            "keywords",
            "moderation_action",
            "automation_trigger",
        ]
        labels = {
            "match_type": "When a comment is…",
            "moderation_action": "Do this to the comment",
            "automation_trigger": "And in Akilent",
        }
        help_texts = {
            "keywords": "Required for 'Keyword / phrase match'; on the other types they're checked as well as the built-in detection.",
        }
        widgets = {"keywords": forms.TextInput()}

    def clean(self):
        data = super().clean() or {}
        if data.get("match_type") == ModerationRule.MatchType.KEYWORD and not (
            data.get("keywords") or ""
        ).strip(" ,"):
            self.add_error("keywords", "Add at least one keyword.")
        if (
            data.get("moderation_action") == ModerationRule.ModerationAction.NONE
            and data.get("automation_trigger") == ModerationRule.AutomationTrigger.NONE
        ):
            raise forms.ValidationError("Choose at least one thing for the rule to do.")
        return data


_KINDS: dict[
    str, tuple[type[CommentTrigger | ModerationRule], type[_ChannelsMixin]]
] = {
    "reply": (CommentTrigger, CommentTriggerForm),
    "moderation": (ModerationRule, ModerationRuleForm),
}


def _style(form: forms.BaseForm) -> forms.BaseForm:
    # The design-system class (.input / .select / .textarea) comes from render_field
    # in components/form.html; this only makes every field full width.
    for field in form.fields.values():
        field.widget.attrs.setdefault("class", "w-full")
    return form


@login_required
@comment_rules_required
def comment_rules(request):
    """List both kinds of rule, with an add form for each."""
    from apps.instagram.services import backfill
    from apps.instagram.services.rule_lint import lint_rules

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    from apps.instagram.services.rule_text import sentence

    lint = lint_rules(account)
    both = len(rule_channels(account)) == 2
    # (rule, its warnings, the rule in a sentence), in the order they run.
    triggers = [
        (rule, lint.for_rule("reply", rule.pk), sentence(rule, show_channels=both))
        for rule in CommentTrigger.objects.filter(account=account)
    ]
    moderation_rules = [
        (rule, lint.for_rule("moderation", rule.pk), sentence(rule, show_channels=both))
        for rule in ModerationRule.objects.filter(account=account)
    ]
    return render(
        request,
        "instagram/comment_rules.html",
        {
            "lint_warnings": lint.page_level,
            "backfill": backfill.get_status(account.pk),
            "triggers": triggers,
            "moderation_rules": moderation_rules,
            # What the rules actually did, so a failure (e.g. a missing Meta
            # permission) is visible to the business, not only in admin.
            "moderation_logs": ModerationLog.objects.filter(account=account)
            .select_related(
                "rule",
                "acted_by",
                "comment__instagram_contact",
                "facebook_comment__facebook_contact",
            )
            .order_by("-occurred_at")[:20],
            "missing_defaults": missing_default_rules(account),
            "has_account": _has_comment_channel(account),
            "both_channels": len(rule_channels(account)) == 2,
            "instagram_only_count": _instagram_only_count(account),
        },
    )


def _instagram_only_rules(account):
    """Rules that skip Facebook comments, for a business that has the Facebook inbox."""
    if "facebook" not in rule_channels(account):
        return []
    return [
        model.objects.filter(account=account, channels=RuleChannels.INSTAGRAM)
        for model in (CommentTrigger, ModerationRule)
    ]


def _instagram_only_count(account) -> int:
    return sum(qs.count() for qs in _instagram_only_rules(account))


@login_required
@comment_rules_required
@require_POST
def comment_rules_extend(request):
    """Make the Instagram-only rules answer Facebook Page comments too."""
    account = get_current_account(request)
    if account is None:
        return redirect("instagram-comment-rules")
    changed = sum(
        qs.update(channels=RuleChannels.BOTH) for qs in _instagram_only_rules(account)
    )
    if changed:
        messages.success(
            request,
            f"{changed} rule{'s' if changed != 1 else ''} now appl{'y' if changed != 1 else 'ies'} "
            "to Facebook comments too.",
        )
    return redirect("instagram-comment-rules")


def _has_comment_channel(account) -> bool:
    from apps.facebook.models.page import active_page

    channels = rule_channels(account)
    return (
        "instagram" in channels
        and account.instagram_accounts.filter(is_active=True).exists()
    ) or ("facebook" in channels and active_page(account) is not None)


@login_required
@comment_rules_required
def comment_rule_edit(request, kind: str, pk: int | None = None):
    """Create (no ``pk``) or edit one rule of ``kind`` ("reply" or "moderation")."""
    account = get_current_account(request)
    if account is None or kind not in _KINDS:
        return redirect("instagram-comment-rules")
    model, form_class = _KINDS[kind]
    instance = get_object_or_404(model, pk=pk, account=account) if pk else None

    from apps.core.htmx import overlay_done, overlay_or_page
    from apps.core.reorder import next_priority

    status = 200
    if request.method == "POST":
        form = form_class(request.POST, instance=instance)
        form.setup_channels(rule_channels(account))
        if form.is_valid():
            rule = form.save(commit=False)
            rule.account = account
            if instance is None:  # a new rule runs after the ones already there
                rule.priority = next_priority(model.objects.filter(account=account))
            rule.save()
            return overlay_done(
                request,
                f'Rule "{rule.name}" saved.',
                reverse("instagram-comment-rules"),
            )
        status = 422
    else:
        form = form_class(instance=instance)
        form.setup_channels(rule_channels(account))
    lint = []
    if instance is not None:
        from apps.instagram.services.rule_lint import lint_rules

        lint = lint_rules(account).for_rule(kind, instance.pk)
    if instance is not None:
        title = f"Edit “{instance.name}”"
    else:
        title = "New private-reply rule" if kind == "reply" else "New moderation rule"
    return overlay_or_page(
        request,
        "instagram/_comment_rule_form.html",
        "instagram/comment_rule_form.html",
        {
            "form": _style(form),
            "kind": kind,
            "rule": instance,
            "lint": lint,
            "title": title,
        },
        kind="drawer",
        title=title,
        status=status,
    )


@login_required
@comment_rules_required
@require_POST
def comment_rules_reorder(request, kind: str):
    """Put one kind of rule in a new order: ``order`` (pks, top first) from a drag, or
    ``move`` (up/down) and ``pk`` from a row's Move buttons."""
    from apps.core import reorder

    account = get_current_account(request)
    if account is None or kind not in _KINDS:
        return redirect("instagram-comment-rules")
    qs = _KINDS[kind][0].objects.filter(account=account)
    if request.POST.get("move"):
        reorder.move(qs, request.POST.get("pk") or 0, request.POST["move"])
    else:
        reorder.renumber(qs, request.POST.getlist("order"))
    return redirect(reverse("instagram-comment-rules") + f"#{kind}-rules")


@login_required
@comment_rules_required
@require_POST
def comment_rule_defaults(request):
    """Add the recommended moderation rules the account doesn't have yet."""
    account = get_current_account(request)
    if account is None:
        return redirect("instagram-comment-rules")
    added = add_default_moderation_rules(account)
    if added:
        messages.success(
            request,
            f"Added {added} recommended rule{'s' if added != 1 else ''}. Edit or pause them any time.",
        )
    else:
        messages.info(request, "You already have all the recommended rules.")
    return redirect("instagram-comment-rules")


@login_required
@comment_rules_required
@require_POST
def comment_rule_toggle(request, kind: str, pk: int):
    account = get_current_account(request)
    if account is None or kind not in _KINDS:
        return redirect("instagram-comment-rules")
    rule = cast(
        "CommentTrigger | ModerationRule",
        get_object_or_404(_KINDS[kind][0], pk=pk, account=account),
    )
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active", "updated_at"])
    from apps.accounts.utils import flash
    from apps.core.undo import undo_url

    flash(
        request,
        "success",
        f'Rule "{rule.name}" {"switched on" if rule.is_active else "paused"}.',
        # Pausing by mistake is the costly one: comments go unanswered meanwhile.
        undo=None
        if rule.is_active
        else undo_url(request, "rule_pause", rule.pk, kind=kind),
    )
    return redirect("instagram-comment-rules")


@login_required
@comment_rules_required
@require_POST
def comment_rule_delete(request, kind: str, pk: int):
    account = get_current_account(request)
    if account is None or kind not in _KINDS:
        return redirect("instagram-comment-rules")
    rule = cast(
        "CommentTrigger | ModerationRule",
        get_object_or_404(_KINDS[kind][0], pk=pk, account=account),
    )
    name = rule.name
    rule.delete()
    messages.success(request, f'Rule "{name}" deleted.')
    return redirect("instagram-comment-rules")


# ── Overlaps: merge one moderation rule into another ────────────────────────


@login_required
@comment_rules_required
def comment_rule_merge(request, pk: int):
    """Merge moderation rule ``pk`` into ``?into=``: a before → after preview first
    (GET, a modal), then the merge (POST). The merged rule is turned off, not deleted."""
    from apps.core.htmx import overlay_done, overlay_or_page
    from apps.instagram.services.rule_lint import apply_merge, merge_preview

    account = get_current_account(request)
    if account is None:
        return redirect("instagram-comment-rules")
    source = get_object_or_404(ModerationRule, pk=pk, account=account)
    into = request.POST.get("into") or request.GET.get("into") or ""
    target = get_object_or_404(
        ModerationRule, pk=int(into) if into.isdigit() else 0, account=account
    )
    if target.pk == source.pk:
        return redirect("instagram-comment-rules")

    if request.method == "POST":
        apply_merge(source, target)
        return overlay_done(
            request,
            f"Merged “{source.name}” into “{target.name}”. “{source.name}” is now paused.",
            reverse("instagram-comment-rules"),
        )
    return overlay_or_page(
        request,
        "instagram/_comment_rule_merge.html",
        "instagram/comment_rule_merge.html",
        {
            "source": source,
            "target": target,
            "after": merge_preview(source, target),
        },
        kind="modal",
        title="Merge two rules",
    )


# ── Apply rules to existing comments (moderation only) ──────────────────────


@login_required
@comment_rules_required
def comment_rules_apply_existing(request):
    """GET: what applying the moderation rules to the last 30 days of comments would do
    (a modal). POST: run it in the background. Hides or flags only: no messages, emails
    or leads."""
    from apps.core.htmx import overlay_done, overlay_or_page
    from apps.instagram.services import backfill
    from apps.instagram.tasks import apply_rules_to_existing

    account = get_current_account(request)
    if account is None:
        return redirect("instagram-comment-rules")

    if request.method == "POST":
        if not backfill.take_lock(account.pk):
            return overlay_done(
                request,
                "Your rules are already being applied to older comments.",
                reverse("instagram-comment-rules"),
                level="info",
            )
        backfill.set_status(account.pk, {"state": "running"}, 60 * 60)
        apply_rules_to_existing.delay(account.pk, backfill.DAYS)
        return overlay_done(
            request,
            "Applying your rules to older comments. The result shows on this page.",
            reverse("instagram-comment-rules"),
        )
    return overlay_or_page(
        request,
        "instagram/_comment_rules_apply_existing.html",
        "instagram/comment_rules_apply_existing.html",
        {"preview": backfill.preview(account)},
        kind="modal",
        title="Apply rules to existing comments",
    )


@login_required
@comment_rules_required
def comment_rules_apply_existing_status(request):
    """The progress card, polled while the background job runs."""
    from apps.instagram.services import backfill

    account = get_current_account(request)
    if account is None:
        return redirect("instagram-comment-rules")
    return render(
        request,
        "instagram/_comment_rules_backfill_status.html",
        {"backfill": backfill.get_status(account.pk)},
    )
