"""Tests for the shared UI component tags (apps/core/templatetags/ui.py)."""

from django import forms
from django.template import Context, Template
from django.test import SimpleTestCase


def render(src: str, ctx: dict | None = None) -> str:
    return Template("{% load ui %}" + src).render(Context(ctx or {}))


class ModalDropdownTests(SimpleTestCase):
    def test_modal_wraps_slot_and_wires_open_event(self):
        out = render(
            '{% modal "invite" title="Invite a teammate" %}<p id="x">hi</p>{% endmodal %}'
        )
        self.assertIn("Invite a teammate", out)
        self.assertIn('id="x"', out)
        self.assertIn("modal-open", out)
        self.assertIn("var(--z-modal)", out)

    def test_modal_size_maps_to_max_width(self):
        out = render('{% modal "m" size="sm" %}body{% endmodal %}')
        self.assertIn("max-w-sm", out)

    def test_dropdown_renders_menu_rows(self):
        out = render('{% dropdown label="Actions" %}<a>Edit</a>{% enddropdown %}')
        self.assertIn("Actions", out)
        self.assertIn("<a>Edit</a>", out)
        self.assertIn("var(--z-dropdown)", out)


class FormFieldTests(SimpleTestCase):
    class DemoForm(forms.Form):
        name = forms.CharField()
        plan = forms.ChoiceField(choices=[("a", "A")])
        bio = forms.CharField(widget=forms.Textarea, required=False)
        agree = forms.BooleanField(label="I agree")

    def test_render_field_picks_widget_class(self):
        f = self.DemoForm()
        self.assertIn(
            'class="input"', render("{% render_field form.name %}", {"form": f})
        )
        self.assertIn("select", render("{% render_field form.plan %}", {"form": f}))
        self.assertIn("textarea", render("{% render_field form.bio %}", {"form": f}))

    def test_render_field_marks_errors(self):
        f = self.DemoForm(data={"name": "", "plan": "a", "agree": "on"})
        f.is_valid()
        out = render("{% render_field form.name %}", {"form": f})
        self.assertIn("input-error", out)
        self.assertIn('aria-invalid="true"', out)


class ModalContractTests(SimpleTestCase):
    """UI quality programme 0.5: one owner for focus, Esc and inertness."""

    def test_modal_is_named_by_its_visible_heading(self):
        out = render('{% modal "invite" title="Invite a teammate" %}x{% endmodal %}')
        self.assertIn('aria-labelledby="modal-invite-title"', out)
        self.assertIn('id="modal-invite-title"', out)

    def test_modal_joins_the_dialog_stack_and_leaves_focus_to_it(self):
        out = render('{% modal "m" title="T" %}x{% endmodal %}')
        self.assertIn("akDialog.open", out)
        self.assertIn("akDialog.close", out)
        # x-trap must not also move focus, or two owners fight over it.
        self.assertIn("x-trap.noscroll.noautofocus.noreturn", out)
        # Esc is handled by the stack (topmost only), not by every modal on window.
        self.assertNotIn("keydown.escape.window", out)


class MobileFieldHintTests(SimpleTestCase):
    """render_field gives phones the right keyboard and autofill."""

    class F(forms.Form):
        email = forms.EmailField()
        whatsapp_number = forms.CharField()
        phonetic = forms.CharField()
        first_name = forms.CharField()
        amount = forms.DecimalField(widget=forms.NumberInput(attrs={"step": "0.01"}))
        seats = forms.IntegerField()
        q = forms.CharField()
        password1 = forms.CharField(widget=forms.PasswordInput)
        custom = forms.CharField(widget=forms.TextInput(attrs={"autocomplete": "off"}))

    def field(self, name):
        return render("{% render_field f." + name + " %}", {"f": self.F()})

    def test_email(self):
        out = self.field("email")
        self.assertIn('inputmode="email"', out)
        self.assertIn('autocomplete="email"', out)

    def test_phone_by_name_part(self):
        out = self.field("whatsapp_number")
        self.assertIn('inputmode="tel"', out)
        self.assertIn('autocomplete="tel"', out)
        self.assertNotIn("inputmode", self.field("phonetic"))

    def test_names_numbers_search_password(self):
        self.assertIn('autocomplete="given-name"', self.field("first_name"))
        self.assertIn('inputmode="decimal"', self.field("amount"))
        self.assertIn('inputmode="numeric"', self.field("seats"))
        self.assertIn('enterkeyhint="search"', self.field("q"))
        self.assertIn('autocomplete="new-password"', self.field("password1"))

    def test_widget_attributes_win(self):
        out = self.field("custom")
        self.assertIn('autocomplete="off"', out)
        self.assertEqual(out.count("autocomplete="), 1)


class DrawerTests(SimpleTestCase):
    def test_drawer_shares_the_modal_contract(self):
        out = render(
            '{% drawer "filters" title="Filters" %}<p id="b">x</p>{% enddrawer %}'
        )
        self.assertIn('aria-labelledby="drawer-filters-title"', out)
        self.assertIn("akDialog.open", out)
        self.assertIn("modal-open", out)
        self.assertIn("var(--z-drawer)", out)
        self.assertIn('id="b"', out)

    def test_drawer_is_full_width_on_phones(self):
        out = render('{% drawer "d" title="T" size="lg" %}x{% enddrawer %}')
        self.assertIn("w-full sm:max-w-lg", out)
