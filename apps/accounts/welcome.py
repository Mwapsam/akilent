"""The optional "Tell us about your business" card on the dashboard.

Signup only asks for what's needed to start (name, email, password, company). Industry and team
size are asked here instead: skippable, shown to an owner or admin until both are answered or
the card is dismissed (kept in automation.DismissedSuggestion, like the setup suggestions).

The choices live on the form only. Account.industry and Account.company_size stay free text,
so values typed at the old signup never fail validation: the card shows one that isn't a choice
as "Other" and only overwrites a field the person actually changes.
"""

from django import forms

DISMISS_KEY = "welcome:profile"
DISMISS_DAYS = 3650  # "No thanks" means it

OTHER = "other"

INDUSTRY_CHOICES = (
    ("Retail & e-commerce", "Retail & e-commerce"),
    ("Food & hospitality", "Food & hospitality"),
    ("Health & beauty", "Health & beauty"),
    ("Education & training", "Education & training"),
    ("Professional services", "Professional services"),
    ("Real estate", "Real estate"),
    ("Travel & events", "Travel & events"),
    ("Software & technology", "Software & technology"),
    ("Non-profit", "Non-profit"),
    (OTHER, "Something else"),
)

TEAM_SIZE_CHOICES = (
    ("Just me", "Just me"),
    ("2-5", "2 to 5 people"),
    ("6-20", "6 to 20 people"),
    ("21-100", "21 to 100 people"),
    ("100+", "More than 100"),
    (OTHER, "Something else"),
)


def _initial(value: str, choices) -> str:
    keys = {key for key, _ in choices}
    if not value:
        return ""
    return value if value in keys else OTHER


class WelcomeProfileForm(forms.Form):
    industry = forms.ChoiceField(
        choices=(("", "Choose your industry"),) + INDUSTRY_CHOICES, required=False
    )
    company_size = forms.ChoiceField(
        label="Team size",
        choices=(("", "Choose team size"),) + TEAM_SIZE_CHOICES,
        required=False,
    )

    def __init__(self, *args, account=None, **kwargs):
        self.account = account
        if account is not None and "initial" not in kwargs:
            kwargs["initial"] = {
                "industry": _initial(account.industry, INDUSTRY_CHOICES),
                "company_size": _initial(account.company_size, TEAM_SIZE_CHOICES),
            }
        super().__init__(*args, **kwargs)

    def save(self) -> list[str]:
        """Write what was changed. "Other" keeps an existing free-text value (it's what the
        card showed it as) and is stored as "Other" only over a blank."""
        changed = []
        for name in ("industry", "company_size"):
            picked = self.cleaned_data.get(name) or ""
            current = getattr(self.account, name) or ""
            if not picked or picked == self.initial.get(name):
                continue
            value = "Other" if picked == OTHER else picked
            if picked == OTHER and current:
                continue
            setattr(self.account, name, value)
            changed.append(name)
        if changed:
            self.account.save(update_fields=changed)
        return changed


def show_card(account, membership) -> bool:
    """An owner or admin of an account missing either answer, who hasn't said no thanks."""
    from apps.accounts.models import Membership
    from apps.automation.patterns import dismissed_keys

    if account is None or membership is None:
        return False
    if membership.role not in (Membership.Role.OWNER, Membership.Role.ADMIN):
        return False
    if account.industry and account.company_size:
        return False
    return DISMISS_KEY not in dismissed_keys(account)


def dismiss(account) -> None:
    from apps.automation.patterns import dismiss as dismiss_suggestion

    dismiss_suggestion(account, DISMISS_KEY, days=DISMISS_DAYS)
