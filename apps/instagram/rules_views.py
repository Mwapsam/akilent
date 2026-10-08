"""Business-facing comment rules: automatic private replies and comment moderation.

Until these pages existed the rules could only be set up in Django admin, so a
business (or Meta's App Reviewer) had no way to use comment features at all.
"""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.core.module_gate import module_required
from apps.instagram.models import CommentTrigger, ModerationRule


class CommentTriggerForm(forms.ModelForm):
    class Meta:
        model = CommentTrigger
        fields = ["name", "match_type", "keywords", "reply_template", "priority"]
        labels = {
            "match_type": "Reply when a comment…",
            "keywords": "Keywords",
            "reply_template": "Private reply",
            "priority": "Order",
        }
        help_texts = {
            "keywords": "Comma-separated, e.g. price, how much, cost. Used with “contains a keyword”.",
            "reply_template": "Sent once as a DM to the commenter. {username} becomes their Instagram name.",
            "priority": "Lower runs first; only the first matching rule replies.",
        }
        widgets = {
            "keywords": forms.TextInput(),
            "reply_template": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        data = super().clean()
        if data.get("match_type") == CommentTrigger.MatchType.KEYWORD and not (
            data.get("keywords") or ""
        ).strip(" ,"):
            self.add_error("keywords", "Add at least one keyword.")
        return data


class ModerationRuleForm(forms.ModelForm):
    class Meta:
        model = ModerationRule
        fields = [
            "name",
            "match_type",
            "keywords",
            "moderation_action",
            "automation_trigger",
            "priority",
        ]
        labels = {
            "match_type": "When a comment is…",
            "moderation_action": "Do this to the comment",
            "automation_trigger": "And in Akilent",
            "priority": "Order",
        }
        help_texts = {
            "keywords": "Comma-separated. Used with “keyword / phrase match”.",
            "priority": "Lower runs first.",
        }
        widgets = {"keywords": forms.TextInput()}

    def clean(self):
        data = super().clean()
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


_KINDS = {
    "reply": (CommentTrigger, CommentTriggerForm),
    "moderation": (ModerationRule, ModerationRuleForm),
}


def _style(form: forms.Form) -> forms.Form:
    for field in form.fields.values():
        widget = field.widget
        css = "select w-full" if isinstance(widget, forms.Select) else "input w-full"
        widget.attrs.setdefault("class", css)
    return form


@login_required
@module_required("instagram")
def comment_rules(request):
    """List both kinds of rule, with an add form for each."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    return render(
        request,
        "instagram/comment_rules.html",
        {
            "triggers": CommentTrigger.objects.filter(account=account),
            "moderation_rules": ModerationRule.objects.filter(account=account),
            "has_account": account.instagram_accounts.filter(is_active=True).exists(),
        },
    )


@login_required
@module_required("instagram")
def comment_rule_edit(request, kind: str, pk: int | None = None):
    """Create (no ``pk``) or edit one rule of ``kind`` ("reply" or "moderation")."""
    account = get_current_account(request)
    if account is None or kind not in _KINDS:
        return redirect("instagram-comment-rules")
    model, form_class = _KINDS[kind]
    instance = get_object_or_404(model, pk=pk, account=account) if pk else None

    if request.method == "POST":
        form = form_class(request.POST, instance=instance)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.account = account
            rule.save()
            messages.success(request, f"Rule “{rule.name}” saved.")
            return redirect("instagram-comment-rules")
    else:
        form = form_class(instance=instance)
    return render(
        request,
        "instagram/comment_rule_form.html",
        {"form": _style(form), "kind": kind, "rule": instance},
    )


@login_required
@module_required("instagram")
@require_POST
def comment_rule_toggle(request, kind: str, pk: int):
    account = get_current_account(request)
    if account is None or kind not in _KINDS:
        return redirect("instagram-comment-rules")
    rule = get_object_or_404(_KINDS[kind][0], pk=pk, account=account)
    rule.is_active = not rule.is_active
    rule.save(update_fields=["is_active", "updated_at"])
    messages.success(
        request, f"Rule “{rule.name}” {'switched on' if rule.is_active else 'paused'}."
    )
    return redirect("instagram-comment-rules")


@login_required
@module_required("instagram")
@require_POST
def comment_rule_delete(request, kind: str, pk: int):
    account = get_current_account(request)
    if account is None or kind not in _KINDS:
        return redirect("instagram-comment-rules")
    rule = get_object_or_404(_KINDS[kind][0], pk=pk, account=account)
    name = rule.name
    rule.delete()
    messages.success(request, f"Rule “{name}” deleted.")
    return redirect("instagram-comment-rules")
