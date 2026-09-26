"""The platform admin's payment-requests page renders, including a pending request's reject dialog."""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account
from apps.billing.models import ManualPaymentRequest, Plan


@pytest.mark.django_db
def test_pending_requests_render_with_a_reject_dialog(client):
    staff = User.objects.create_user("staff", "staff@example.com", "pw", is_staff=True, is_superuser=True)
    client.force_login(staff)
    account = Account.objects.create(company_name="Sunrise Solar")
    plan = Plan.objects.create(name="Growth", slug="growth", price_monthly=100)
    req = ManualPaymentRequest.objects.create(account=account, plan=plan, reference="MOMO-123")

    html = client.get("/manage/payments/").content.decode()
    assert "MOMO-123" in html
    assert f"reject-{req.pk}" in html and "Reject payment request" in html
