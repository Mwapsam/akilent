import pytest

from apps.accounts.models import Account
from apps.email.models import EmailTemplate, TemplateComponent
from apps.email.services.render import render_string, render_template


def test_builtin_button_renders_with_kwargs():
    out = render_string('{% component "AkilentButton" href="https://acme.com/pay" label="Pay now" %}')
    assert 'href="https://acme.com/pay"' in out
    assert "Pay now" in out


def test_builtin_component_escapes_values():
    out = render_string('{% component "AkilentHeader" title="<script>x</script>" %}')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_component_works_alongside_conditionals_and_vars():
    src = (
        "Hi {{ name }}."
        "{% if overdue %}{% component \"AkilentButton\" href=url label=\"Pay overdue\" %}"
        "{% else %}Thanks!{% endif %}"
    )
    out = render_string(src, {"name": "Ada", "overdue": True, "url": "https://x/y"})
    assert "Hi Ada." in out and 'href="https://x/y"' in out


def test_unknown_component_raises():
    with pytest.raises(Exception):
        render_string('{% component "Nope" %}')


@pytest.mark.django_db
def test_account_custom_component_available_in_render_template():
    acc = Account.objects.create(company_name="Acme")
    TemplateComponent.objects.create(
        account=acc, name="Signoff", html='<p>— {{ who }}, {{ team }}</p>'
    )
    tpl = EmailTemplate.objects.create(
        account=acc, name="T", slug="t",
        subject="Hi", text_body="",
        html_body='{% component "Signoff" who="Sam" team="Acme" %}',
    )
    _, _, html = render_template(tpl, {})
    assert "— Sam, Acme" in html
