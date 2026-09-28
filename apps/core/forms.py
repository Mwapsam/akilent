from django import forms

from .models import Configurations


class ConfigurationForm(forms.ModelForm):
    class Meta:
        model = Configurations
        fields = ["name", "value", "is_secret"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "input",
                    "placeholder": "Configuration name",
                    "autocomplete": "off",
                }
            ),
            "value": forms.Textarea(
                attrs={
                    "class": "input",
                    "placeholder": "Configuration value",
                    "rows": 4,
                }
            ),
            "is_secret": forms.CheckboxInput(
                attrs={
                    "class": "rounded border-border-strong text-brand-600 focus:ring-brand-500",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # If editing an existing configuration that is marked as secret
        if self.instance and self.instance.pk and self.instance.is_secret:
            # 1. Mask the value in the UI so it doesn't show plaintext
            self.initial["value"] = "********"
            # 2. Make the field optional so they can save without changing it
            self.fields["value"].required = False
            # 3. Update the placeholder to give the user a hint
            self.fields["value"].widget.attrs["placeholder"] = (
                "Enter new value or leave as ******** to keep current"
            )

    def clean_value(self):
        value = self.cleaned_data.get("value")

        # If editing an existing secret configuration
        if self.instance and self.instance.pk and self.instance.is_secret:
            # If the user left the masked value or left it blank
            if value in (None, "", "********"):
                # Return the original decrypted value.
                # The EncryptedTextField will transparently re-encrypt it on save.
                return self.instance.value

        return value

    def add_error_classes(self):
        """Call after is_valid() in the view to flag invalid fields."""
        for name in self.errors:
            field = self.fields.get(name)
            if field:
                current_classes = field.widget.attrs.get("class", "")
                # Prevent adding the class multiple times if the form is re-rendered
                if "input-error" not in current_classes:
                    field.widget.attrs["class"] = (
                        f"{current_classes} input-error".strip()
                    )
