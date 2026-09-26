"""template_create view — slug collisions must not 500."""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.email.models import EmailTemplate


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.mark.django_db
def test_repeated_name_suffixes_slug_instead_of_500(client, account, monkeypatch):
    monkeypatch.setattr("apps.billing.limits.LimitChecker.has_feature", lambda self, name: True)
    client.force_login(account.owner)

    def post():
        return client.post("/email/templates/create/", {"name": "Quarterly Update"})

    assert post().status_code == 302
    assert post().status_code == 302   # would have been a 500 before the fix
    assert post().status_code == 302

    slugs = sorted(
        EmailTemplate.objects.filter(account=account, name="Quarterly Update")
        .values_list("slug", flat=True)
    )
    assert slugs == ["quarterly-update", "quarterly-update-2", "quarterly-update-3"]


@pytest.mark.django_db
def test_collides_with_existing_starter_template(client, account, monkeypatch):
    monkeypatch.setattr("apps.billing.limits.LimitChecker.has_feature", lambda self, name: True)
    client.force_login(account.owner)

    existing = EmailTemplate.objects.filter(account=account).first()
    assert existing is not None  # starter templates are seeded on account creation

    resp = client.post(
        "/email/templates/create/", {"name": existing.name, "slug": existing.slug}
    )
    assert resp.status_code == 302
    assert EmailTemplate.objects.filter(
        account=account, slug=f"{existing.slug}-2"
    ).exists()
