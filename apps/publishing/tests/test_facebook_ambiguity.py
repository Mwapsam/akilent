from unittest.mock import MagicMock, patch

import pytest
import requests

from apps.accounts.models import Account
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import publish_variant
from apps.publishing.tasks import publish_variant_async

pytestmark = pytest.mark.django_db


@pytest.fixture
def fb_variant():
    account = Account.objects.create(company_name="FB Co")
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], media_type="image"
    )
    return ContentPostVariant.objects.create(
        post=post, platform="facebook", caption="hi"
    )


def _provider(error):
    provider = MagicMock()
    provider.create_photo_post.side_effect = error
    return provider


def test_facebook_publish_that_may_have_landed_is_reconciled_not_failed(fb_variant):
    provider = _provider(requests.ConnectionError("Connection reset after send"))
    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "page"),
    ):
        publish_variant(ContentPostVariant.objects.get(pk=fb_variant.pk))

    fb_variant.refresh_from_db()
    assert fb_variant.status == ContentPostVariant.Status.RECONCILING


def test_ambiguous_facebook_publish_is_never_automatically_republished(fb_variant):
    provider = _provider(requests.ConnectionError("Connection reset after send"))
    with (
        patch(
            "apps.publishing.services._fb_provider_for_post",
            return_value=(provider, "page"),
        ),
        patch.object(publish_variant_async, "retry") as retry,
    ):
        publish_variant_async.run(fb_variant.pk)

    assert provider.create_photo_post.call_count == 1
    retry.assert_not_called()


def test_definitive_facebook_rejection_is_still_failed(fb_variant):
    from apps.publishing.providers.facebook import FacebookPublishError

    provider = _provider(FacebookPublishError("Invalid image"))
    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "page"),
    ):
        publish_variant(ContentPostVariant.objects.get(pk=fb_variant.pk))

    fb_variant.refresh_from_db()
    assert fb_variant.status == ContentPostVariant.Status.FAILED
