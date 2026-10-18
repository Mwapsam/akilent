from unittest.mock import MagicMock, patch

import pytest
import requests

from apps.accounts.models import Account
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.providers.instagram import InstagramPublishError
from apps.publishing.services import publish_variant
from apps.publishing.tasks import publish_variant_async

pytestmark = pytest.mark.django_db


@pytest.fixture
def ig_variant():
    account = Account.objects.create(company_name="IG Co")
    post = ContentPost.objects.create(
        account=account,
        platforms=["instagram"],
        media_type="image",
        media_urls=["https://cdn.example.com/a.jpg"],
    )
    return ContentPostVariant.objects.create(
        post=post, platform="instagram", caption="hi"
    )


def _provider(publish_error):
    provider = MagicMock()
    provider.create_image_container.return_value = "container-1"
    provider.publish_container.side_effect = publish_error
    return provider


def _patched(provider):
    ig = MagicMock(instagram_business_account_id="ig-user")
    return (
        patch("apps.publishing.services._ig_provider_for_post", return_value=provider),
        patch("apps.publishing.services._ig_account", return_value=ig),
        patch("apps.publishing.services._validate_ig_image_ratio"),
        patch("apps.publishing.services._wait_for_ig_container"),
    )


def test_instagram_publish_that_may_have_landed_is_reconciled(ig_variant):
    provider = _provider(requests.ConnectionError("Connection reset after send"))
    p1, p2, p3, p4 = _patched(provider)
    with p1, p2, p3, p4:
        publish_variant(ContentPostVariant.objects.get(pk=ig_variant.pk))

    ig_variant.refresh_from_db()
    assert ig_variant.status == ContentPostVariant.Status.RECONCILING
    assert ig_variant.metadata.get("ig_container_id") == "container-1"


def test_ambiguous_instagram_publish_is_never_automatically_republished(ig_variant):
    provider = _provider(KeyError("id"))
    p1, p2, p3, p4 = _patched(provider)
    with p1, p2, p3, p4, patch.object(publish_variant_async, "retry") as retry:
        publish_variant_async.run(ig_variant.pk)

    assert provider.publish_container.call_count == 1
    retry.assert_not_called()


def test_definitive_instagram_rejection_is_still_failed(ig_variant):
    provider = _provider(InstagramPublishError("Media rejected"))
    p1, p2, p3, p4 = _patched(provider)
    with p1, p2, p3, p4:
        publish_variant(ContentPostVariant.objects.get(pk=ig_variant.pk))

    ig_variant.refresh_from_db()
    assert ig_variant.status == ContentPostVariant.Status.FAILED


def test_validation_failure_is_not_automatically_retried(ig_variant):
    with (
        patch("apps.publishing.services._ig_provider_for_post", return_value=None),
        patch.object(publish_variant_async, "retry") as retry,
    ):
        assert publish_variant_async.run(ig_variant.pk) == "failed"

    retry.assert_not_called()
