import datetime
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
)
from apps.publishing.providers.errors import MetaPublishError
from apps.publishing.providers.facebook import FacebookPublishError
from apps.publishing.providers.instagram import InstagramPublishError
from apps.publishing.services import (
    FB_RECONNECT_MESSAGE,
    IG_RECONNECT_MESSAGE,
    _validate_ig_image_ratio,
    publish_variant,
)
from apps.publishing.tasks import (
    poll_ig_variant_containers,
    publish_post_async,
    publish_variant_async,
)

pytestmark = pytest.mark.django_db
ErrorType = ContentPostVariant.ErrorType


@pytest.fixture
def owner_client():
    account = Account.objects.create(company_name="Harden Co")
    user = User.objects.create_user("owner", "owner@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return client, account


def _fb_variant(account):
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], media_type="text"
    )
    FacebookPageConnection.objects.create(
        account=account, page_id="p1", access_token="tok"
    )
    return ContentPostVariant.objects.create(post=post, platform="facebook")


# --- Graph error classification ------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"code": 190}, ErrorType.PERMISSION),
        ({"http_status": 401}, ErrorType.PERMISSION),
        ({"code": 10}, ErrorType.PERMISSION),
        ({"code": 200}, ErrorType.PERMISSION),
        ({"code": 4}, ErrorType.RATE_LIMIT),
        ({"code": 80004}, ErrorType.RATE_LIMIT),
        ({"http_status": 429}, ErrorType.RATE_LIMIT),
        ({"code": 100}, ErrorType.VALIDATION),
        ({"code": 36003}, ErrorType.VALIDATION),
        ({"code": 2}, ErrorType.PROVIDER_OUTAGE),
        ({"http_status": 503}, ErrorType.PROVIDER_OUTAGE),
        ({}, ErrorType.UNKNOWN),
    ],
)
def test_graph_errors_are_classified(kwargs, expected):
    assert MetaPublishError("x", **kwargs).error_type == expected


def test_error_is_built_from_a_graph_response():
    response = MagicMock(status_code=400)
    exc = FacebookPublishError.from_response(
        response, {"error": {"message": "Bad token", "code": 190, "error_subcode": 463}}
    )
    assert (exc.code, exc.subcode, exc.http_status) == (190, 463, 400)
    assert exc.token_invalid
    assert str(exc) == "Bad token"


# --- Expired tokens -------------------------------------------------------------


def test_rejected_facebook_token_marks_the_page_for_reconnect(owner_client):
    _, account = owner_client
    variant = _fb_variant(account)
    provider = MagicMock()
    provider.create_text_post.side_effect = FacebookPublishError("expired", code=190)

    with (
        patch(
            "apps.publishing.services._fb_provider_for_post",
            return_value=(provider, "p1"),
        ),
        patch.object(publish_variant_async, "retry") as retry,
    ):
        publish_variant_async.run(variant.pk)

    variant.refresh_from_db()
    assert variant.status == ContentPostVariant.Status.FAILED
    assert variant.last_error_type == ErrorType.PERMISSION
    assert variant.last_error == FB_RECONNECT_MESSAGE
    assert FacebookPageConnection.objects.get(account=account).status == "expired"
    retry.assert_not_called()


def test_rate_limited_facebook_publish_is_retried(owner_client):
    _, account = owner_client
    variant = _fb_variant(account)
    provider = MagicMock()
    provider.create_text_post.side_effect = FacebookPublishError("slow down", code=4)

    with (
        patch(
            "apps.publishing.services._fb_provider_for_post",
            return_value=(provider, "p1"),
        ),
        patch.object(publish_variant_async, "retry", side_effect=RuntimeError) as retry,
        pytest.raises(RuntimeError),
    ):
        publish_variant_async.run(variant.pk)

    retry.assert_called_once()
    variant.refresh_from_db()
    assert variant.last_error_type == ErrorType.RATE_LIMIT


def _ig_variant(account, token_expired=False):
    from apps.instagram.models import InstagramBusinessAccount

    ig = InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id="igu",
        access_token="tok",
        verify_token="v",
        token_expired=token_expired,
    )
    post = ContentPost.objects.create(
        account=account,
        platforms=["instagram"],
        media_type="image",
        media_urls=["https://cdn.example.com/a.jpg"],
    )
    return ig, ContentPostVariant.objects.create(post=post, platform="instagram")


def test_known_expired_instagram_token_is_not_used(owner_client):
    _, account = owner_client
    _, variant = _ig_variant(account, token_expired=True)

    with patch("apps.publishing.services.InstagramPublishProvider") as provider_cls:
        publish_variant(variant)

    provider_cls.assert_not_called()
    variant.refresh_from_db()
    assert variant.last_error == IG_RECONNECT_MESSAGE


def test_rejected_instagram_token_is_flagged_expired(owner_client):
    _, account = owner_client
    ig, variant = _ig_variant(account)
    provider = MagicMock()
    provider.create_image_container.side_effect = InstagramPublishError(
        "expired", code=190
    )

    with (
        patch("apps.publishing.services._ig_provider_for_post", return_value=provider),
        patch("apps.publishing.services._validate_ig_image_ratio"),
    ):
        publish_variant(variant)

    ig.refresh_from_db()
    assert ig.token_expired


# --- SSRF -----------------------------------------------------------------------


def test_image_check_never_fetches_foreign_hosts():
    with patch("requests.get") as get:
        _validate_ig_image_ratio("http://169.254.169.254/latest/meta-data")
        _validate_ig_image_ratio("file:///etc/passwd")

    get.assert_not_called()


def test_image_check_fetches_our_own_media(settings):
    settings.MEDIA_URL = "https://media.akilent.test/media/"
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"not an image"]
    with patch("requests.get", return_value=response) as get:
        _validate_ig_image_ratio("https://media.akilent.test/media/a.jpg")

    assert get.call_args.kwargs["allow_redirects"] is False


# --- Composer XSS -----------------------------------------------------------------


def test_composer_renders_existing_content_as_inert_json(owner_client):
    client, account = owner_client
    payload = '"onmouseover="alert(1)'
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], hashtags=[payload]
    )

    page = client.get(reverse("publishing:post-new"), {"post_id": post.pk}).text

    assert payload not in page
    assert 'id="post-editor-data"' in page


# --- State machine -------------------------------------------------------------------


def test_final_task_failure_keeps_a_partial_success(owner_client):
    _, account = owner_client
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook", "instagram"],
        status=ContentPost.Status.PUBLISHING,
    )
    ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        status=ContentPostVariant.Status.PUBLISHED,
        publication_id="fb1",
    )
    ContentPostVariant.objects.create(
        post=post, platform="instagram", status=ContentPostVariant.Status.FAILED
    )

    with (
        patch(
            "apps.publishing.services.orchestrate_post_publication",
            side_effect=RuntimeError("boom"),
        ),
        patch.object(publish_post_async, "max_retries", 0),
    ):
        publish_post_async.run(post.pk)

    post.refresh_from_db()
    assert post.status != ContentPost.Status.FAILED
    assert post.fb_post_id == "fb1"


def test_poll_keeps_metadata_written_by_others(owner_client):
    _, account = owner_client
    post = ContentPost.objects.create(account=account, platforms=["instagram"])
    variant = ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=ContentPostVariant.Status.PROCESSING,
        metadata={"ig_container_id": "c1", "claim_token": "t"},
    )

    with patch(
        "apps.publishing.services.try_publish_ig_variant_container", return_value=False
    ):
        poll_ig_variant_containers()

    variant.refresh_from_db()
    assert variant.metadata["claim_token"] == "t"
    assert "last_poll_attempt" in variant.metadata


def test_workflow_does_not_republish_a_published_post(owner_client):
    from apps.automation.models import Workflow, WorkflowRun
    from apps.automation.workflow_engine import _run_publish_post
    from apps.contacts.models import Contact

    _, account = owner_client
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], status=ContentPost.Status.PUBLISHED
    )
    workflow = Workflow.objects.create(account=account, name="wf")
    contact = Contact.objects.create(account=account, phone="+15550000009")
    run = WorkflowRun.objects.create(workflow=workflow, contact=contact)

    with patch("apps.publishing.services.orchestrate_post_publication") as orchestrate:
        result = _run_publish_post(
            run, {"type": "publish_post", "post_id": str(post.pk), "on_error": "x"}
        )

    orchestrate.assert_not_called()
    assert result["reason"] == "invalid_status"


# --- Composer / views ------------------------------------------------------------------


def test_schedule_without_a_date_is_refused_not_silently_drafted(owner_client):
    client, account = owner_client

    client.post(
        reverse("publishing:post-new"),
        {
            "caption": "c",
            "media_type": "text",
            "platforms": ["facebook"],
            "action": "schedule",
        },
    )

    assert not ContentPost.objects.filter(account=account).exists()


# --- Facebook connect ---------------------------------------------------------------------


def _pending_connection(account, minutes_ago=0):
    return FacebookPageConnection.objects.create(
        account=account,
        oauth_state="s1",
        oauth_state_created_at=timezone.now() - datetime.timedelta(minutes=minutes_ago),
        status=FacebookPageConnection.Status.DISCONNECTED,
    )


def _callback(client, pages):
    with (
        patch(
            "apps.publishing.views.requests.get",
            return_value=MagicMock(json=lambda: {"access_token": "short"}),
        ),
        patch(
            "apps.publishing.views.exchange_for_long_lived_user_token",
            return_value="long",
        ),
        patch("apps.publishing.views.list_pages", return_value=pages),
    ):
        return client.get(
            reverse("publishing:fb-callback"), {"code": "c", "state": "s1"}
        )


def test_several_pages_lead_to_a_choice_not_the_first_page(owner_client, settings):
    settings.WHATSAPP_APP_ID = "app"
    client, account = owner_client
    conn = _pending_connection(account)
    pages = [
        {"id": "p1", "name": "One", "access_token": "t1"},
        {"id": "p2", "name": "Two", "access_token": "t2"},
    ]

    response = _callback(client, pages)

    assert response.url == reverse("publishing:fb-select-page")
    conn.refresh_from_db()
    assert not conn.page_id
    assert "long" not in str(client.session["fb_page_choices"])

    with patch(
        "apps.publishing.views.get_page_token", return_value="page-two"
    ) as get_token:
        client.post(reverse("publishing:fb-select-page"), {"page_id": "p2"})

    get_token.assert_called_once_with("long", "p2")
    conn.refresh_from_db()
    assert (conn.page_id, conn.access_token, conn.status) == (
        "p2",
        "page-two",
        "active",
    )


def test_unlisted_page_cannot_be_selected(owner_client, settings):
    settings.WHATSAPP_APP_ID = "app"
    client, account = owner_client
    _pending_connection(account)
    _callback(
        client,
        [
            {"id": "p1", "name": "One", "access_token": "t1"},
            {"id": "p2", "name": "Two", "access_token": "t2"},
        ],
    )

    with patch("apps.publishing.views.get_page_token") as get_token:
        client.post(reverse("publishing:fb-select-page"), {"page_id": "p-evil"})

    get_token.assert_not_called()


def test_stale_oauth_state_is_refused_and_consumed(owner_client, settings):
    settings.WHATSAPP_APP_ID = "app"
    client, account = owner_client
    conn = _pending_connection(account, minutes_ago=30)

    with patch("apps.publishing.views.list_pages") as list_pages:
        _callback(client, [])

    list_pages.assert_not_called()
    conn.refresh_from_db()
    assert conn.oauth_state == ""


# --- Operator alerts ------------------------------------------------------------------------


def test_operator_console_flags_publishing_problems(owner_client):
    from apps.core.console.attention import reasons

    _, account = owner_client
    post = ContentPost.objects.create(account=account, platforms=["facebook"])
    old = ContentPostVariant.objects.create(
        post=post, platform="facebook", status=ContentPostVariant.Status.RECONCILING
    )
    ContentPostVariant.objects.filter(pk=old.pk).update(
        updated_at=timezone.now() - datetime.timedelta(days=2)
    )
    other = ContentPost.objects.create(account=account, platforms=["instagram"])
    ContentPostVariant.objects.create(
        post=other,
        platform="instagram",
        status=ContentPostVariant.Status.FAILED,
        last_error_at=timezone.now(),
    )
    FacebookPageConnection.objects.create(
        account=account, status=FacebookPageConnection.Status.EXPIRED
    )

    why = reasons(account)

    assert "1 post awaiting reconciliation for 1+ day" in why
    assert "1 failed post publish today" in why
    assert "Facebook Page needs reconnecting" in why


def test_fresh_reconciliation_is_left_to_the_business(owner_client):
    from apps.publishing.api import attention_reasons

    _, account = owner_client
    post = ContentPost.objects.create(account=account, platforms=["facebook"])
    ContentPostVariant.objects.create(
        post=post, platform="facebook", status=ContentPostVariant.Status.RECONCILING
    )

    assert attention_reasons(account) == []


# --- Pre-deploy audit -------------------------------------------------------------------------


def test_publishing_audit_fails_on_active_posts_without_variants(owner_client):
    from io import StringIO

    from django.core.management import call_command

    _, account = owner_client
    ContentPost.objects.create(
        account=account, platforms=["facebook"], status=ContentPost.Status.SCHEDULED
    )
    out = StringIO()

    with pytest.raises(SystemExit) as exit_info:
        call_command("publishing_audit", stdout=out)

    assert exit_info.value.code == 1
    assert "status=scheduled" in out.getvalue()


def test_publishing_audit_passes_and_changes_nothing(owner_client):
    from io import StringIO

    from django.core.management import call_command

    _, account = owner_client
    ContentPost.objects.create(account=account, platforms=["facebook"])  # draft
    out = StringIO()

    call_command("publishing_audit", stdout=out)

    assert "No deploy blockers found." in out.getvalue()
    assert ContentPost.objects.count() == 1
