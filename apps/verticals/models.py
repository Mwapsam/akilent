from django.db import models


class VerticalActivation(models.Model):
    """Records that an account has activated a vertical Starter pack.

    Purely a UI/idempotency marker — the real effect of activation is the
    ``ModuleSubscription`` rows enabled and ``Workflow`` rows created/updated
    by ``apps.verticals.services.activate_vertical``. Re-activating the same
    vertical is safe (it re-syncs modules/workflows) and does not create a
    second row here.
    """

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE, related_name="vertical_activations")
    key = models.CharField(max_length=50)
    activated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["account", "key"], name="unique_vertical_activation_per_account"),
        ]

    def __str__(self):
        return f"{self.account} / {self.key}"
