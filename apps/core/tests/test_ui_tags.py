"""Tests for the shared UI component tags (apps/core/templatetags/ui.py)."""

from django import forms
from django.template import Context, Template
from django.test import SimpleTestCase


def render(src: str, ctx: dict | None = None) -> str:
    return Template("{% load ui %}" + src).render(Context(ctx or {}))


class ModalDropdownTests(SimpleTestCase):
    def test_modal_wraps_slot_and_wires_open_event(self):
        out = render('{% modal "invite" title="Invite a teammate" %}<p id="x">hi</p>{% endmodal %}')
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
        self.assertIn('class="input"', render("{% render_field form.name %}", {"form": f}))
        self.assertIn("select", render("{% render_field form.plan %}", {"form": f}))
        self.assertIn("textarea", render("{% render_field form.bio %}", {"form": f}))

    def test_render_field_marks_errors(self):
        f = self.DemoForm(data={"name": "", "plan": "a", "agree": "on"})
        f.is_valid()
        out = render("{% render_field form.name %}", {"form": f})
        self.assertIn("input-error", out)
        self.assertIn('aria-invalid="true"', out)
