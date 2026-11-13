"""Storing and resolving audience signals: idempotent runs, history on edit, owner
corrections, AI only where rules found nothing, erasure, ingest on both channels, and
moderation left untouched (docs/plans/audience-intelligence-spec.md §4)."""

from __future__ import annotations

import secrets
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.audience import services
from apps.audience.classifier import CLASSIFIER_VERSION
from apps.audience.models import (
    AudienceSignal,
    AudienceSignalCorrection,
    AudienceSignalRun,
)
from apps.instagram.models.comment import Comment
from apps.instagram.tests.helpers import (
    make_account,
    make_instagram_account,
    make_instagram_contact,
)
from apps.instagram.tests.test_phase2_acceptance import make_thread_and_comment

pytestmark = pytest.mark.django_db


@pytest.fixture
def ig():
    account, user = make_account()
    ig_account = make_instagram_account(account)
    ig_contact, _ = make_instagram_contact(account, ig_account)
    return account, user, ig_account, ig_contact


def _comment(ig, body):
    _account, _user, ig_account, ig_contact = ig
    _thread, comment = make_thread_and_comment(ig_account, ig_contact, body=body)
    return comment


def _cats(comment):
    return {(s.category, s.subcategory) for s in services.current_signals(comment)}


# --- writing ------------------------------------------------------------------------


def test_a_comment_gets_one_run_with_a_primary_signal(ig):
    comment = _comment(ig, "Love it! how much?")
    run = services.classify_comment(comment)
    assert run.channel == "instagram" and run.account_id == ig[0].pk
    assert run.classifier_version == CLASSIFIER_VERSION and run.source == "rule"
    assert run.occurred_at == comment.timestamp
    primary = run.signals.get(is_primary=True)
    assert primary.category == "buying_intent"
    assert run.signals.count() == 2


def test_classifying_twice_is_idempotent(ig):
    comment = _comment(ig, "how much?")
    first = services.classify_comment(comment)
    assert services.classify_comment(comment) == first
    assert AudienceSignalRun.objects.count() == 1


def test_an_edit_adds_a_run_and_keeps_history(ig):
    comment = _comment(ig, "how much?")
    services.classify_comment(comment)
    comment.body = "never arrived, still waiting"
    comment.save()
    services.classify_comment(comment)
    assert AudienceSignalRun.objects.filter(ig_comment=comment).count() == 2
    assert _cats(comment) == {("complaint", "delivery_delay")}


def test_outbound_and_empty_comments_are_not_classified(ig):
    outbound = _comment(ig, "Thanks! sending details")
    outbound.direction = Comment.Direction.OUTBOUND
    outbound.save()
    assert services.classify_comment(outbound) is None
    assert services.classify_comment(_comment(ig, "  ")) is None
    assert not AudienceSignalRun.objects.exists()


@override_settings(AUDIENCE_SIGNALS_ENABLED=False)
def test_switched_off_writes_nothing(ig):
    assert services.classify_comment(_comment(ig, "how much?")) is None
    assert not AudienceSignalRun.objects.exists()


def test_record_safely_never_raises(ig):
    comment = _comment(ig, "how much?")
    with patch("apps.audience.classifier.classify", side_effect=RuntimeError("boom")):
        services.record_safely(comment)
    assert not AudienceSignalRun.objects.exists()


def test_deleting_the_comment_removes_its_signals(ig):
    comment = _comment(ig, "how much?")
    services.classify_comment(comment)
    services.set_correction(comment, "praise", "product", "add")
    comment.delete()
    assert not AudienceSignalRun.objects.exists()
    assert not AudienceSignal.objects.exists()
    assert not AudienceSignalCorrection.objects.exists()


def test_erasing_the_customer_removes_their_signals(ig):
    from apps.contacts.erasure import delete_customer

    comment = _comment(ig, "how much?")
    services.classify_comment(comment)
    delete_customer(ig[3].contact)
    assert not AudienceSignalRun.objects.exists()


# --- resolving: corrections and AI --------------------------------------------------


def test_owner_can_add_and_reject_categories(ig):
    comment = _comment(ig, "Love it! how much?")
    services.classify_comment(comment)
    services.set_correction(comment, "praise", "", "reject")
    services.set_correction(comment, "product_enquiry", "size", "add")
    assert _cats(comment) == {("buying_intent", "price"), ("product_enquiry", "size")}


def test_a_correction_survives_a_new_classifier_version(ig):
    comment = _comment(ig, "how much?")
    services.classify_comment(comment)
    services.set_correction(comment, "buying_intent", "price", "reject")
    services.set_correction(comment, "complaint", "pricing", "add")
    with patch("apps.audience.classifier.CLASSIFIER_VERSION", "rules-99"):
        services.classify_comment(comment)
    assert AudienceSignalRun.objects.count() == 2
    signals = services.current_signals(comment)
    assert {(s.category, s.subcategory) for s in signals} == {("complaint", "pricing")}
    assert signals[0].source == "owner" and signals[0].is_primary


def test_a_correction_stops_applying_when_the_text_is_edited(ig):
    comment = _comment(ig, "how much?")
    services.classify_comment(comment)
    services.set_correction(comment, "complaint", "pricing", "add")
    comment.body = "love it"
    comment.save()
    services.classify_comment(comment)
    assert _cats(comment) == {("praise", "product")}
    assert AudienceSignalCorrection.objects.count() == 1  # kept as history


def test_unknown_categories_are_refused(ig):
    comment = _comment(ig, "how much?")
    with pytest.raises(ValueError):
        services.set_correction(comment, "sentiment", "", "add")
    with pytest.raises(ValueError):
        services.set_correction(comment, "praise", "nope", "add")


def _ai_run(comment, *pairs):
    run = AudienceSignalRun.objects.create(
        account=comment.thread.instagram_account.account,
        channel="instagram",
        ig_comment=comment,
        source="ai",
        classifier_version="ai-test",
        text_hash=services.text_hash(comment.body),
        occurred_at=comment.timestamp,
    )
    for cat, sub in pairs:
        AudienceSignal.objects.create(run=run, category=cat, subcategory=sub)
    return run


def test_ai_fills_in_only_where_the_rules_found_nothing(ig):
    comment = _comment(ig, "c'est combien?")
    services.classify_comment(comment)
    assert _cats(comment) == {("general", "other")}
    _ai_run(comment, ("buying_intent", "price"))
    signals = services.current_signals(comment)
    assert [(s.category, s.subcategory, s.source) for s in signals] == [
        ("buying_intent", "price", "ai")
    ]


def test_rules_win_over_ai(ig):
    comment = _comment(ig, "love it")
    services.classify_comment(comment)
    _ai_run(comment, ("complaint", "quality"))
    assert _cats(comment) == {("praise", "product")}


# --- the webhook pipeline -----------------------------------------------------------


def _ig_entry(ig_contact, text):
    return {
        "id": f"cmt_{secrets.token_hex(6)}",
        "text": text,
        "from": {"id": ig_contact.instagram_scoped_id},
        "media": {"id": f"post_{secrets.token_hex(6)}"},
        "timestamp": timezone.now().isoformat(),
        "parent_id": "",
    }


def test_an_instagram_comment_is_classified_on_arrival(ig):
    from apps.instagram.services.inbound import _process_comment_entry

    _account, _user, ig_account, ig_contact = ig
    _process_comment_entry(ig_account, _ig_entry(ig_contact, "Do you ship to Paris?"))
    run = AudienceSignalRun.objects.get()
    assert run.ig_comment.body == "Do you ship to Paris?"
    assert {(s.category, s.subcategory) for s in run.signals.all()} == {
        ("product_enquiry", "delivery")
    }


@patch("apps.instagram.providers.meta.MetaInstagramProvider")
def test_moderation_still_runs_when_classification_fails(MockProvider, ig):
    from apps.instagram.models.moderation import ModerationLog
    from apps.instagram.services.inbound import _process_comment_entry
    from apps.instagram.tests.test_phase2_acceptance import make_rule

    MockProvider.return_value.hide_comment.return_value = (True, "")
    account, _user, ig_account, ig_contact = ig
    make_rule(account, keywords="buy")
    with patch("apps.audience.classifier.classify", side_effect=RuntimeError("boom")):
        _process_comment_entry(ig_account, _ig_entry(ig_contact, "I want to buy this"))
    assert ModerationLog.objects.count() == 1
    assert Comment.objects.count() == 1
    assert not AudienceSignalRun.objects.exists()


@patch("apps.instagram.providers.meta.MetaInstagramProvider")
def test_classification_does_not_change_moderation(MockProvider, ig):
    """Same moderation outcome whether audience signals are on or off."""
    from apps.instagram.models.moderation import ModerationLog
    from apps.instagram.services.inbound import _process_comment_entry
    from apps.instagram.tests.test_phase2_acceptance import make_rule

    MockProvider.return_value.hide_comment.return_value = (True, "")
    account, _user, ig_account, ig_contact = ig
    make_rule(account, keywords="crypto")
    outcomes = []
    for enabled in (False, True):
        with override_settings(AUDIENCE_SIGNALS_ENABLED=enabled):
            _process_comment_entry(
                ig_account, _ig_entry(ig_contact, "crypto, how much?")
            )
        log = ModerationLog.objects.order_by("-pk").first()
        outcomes.append(
            (log.moderation_action, log.outcome, log.comment.moderation_state)
        )
    assert outcomes[0] == outcomes[1]


# --- backfill -----------------------------------------------------------------------


def test_backfill_classifies_older_comments_once(ig):
    for body in ("how much?", "love it", "  "):
        _comment(ig, body)
    out = StringIO()
    call_command("audience_backfill", "--dry-run", stdout=out)
    assert "would classify 2 Instagram" in out.getvalue()
    assert not AudienceSignalRun.objects.exists()

    call_command("audience_backfill", stdout=StringIO())
    assert AudienceSignalRun.objects.count() == 2
    out = StringIO()
    call_command("audience_backfill", stdout=out)
    assert "classified 0 Instagram" in out.getvalue()
    assert AudienceSignalRun.objects.count() == 2


def test_backfill_can_be_limited_to_one_account(ig):
    _comment(ig, "how much?")
    other = make_account()[0]
    other_ig = make_instagram_account(other)
    other_contact, _ = make_instagram_contact(other, other_ig)
    make_thread_and_comment(other_ig, other_contact, body="how much?")
    call_command("audience_backfill", "--account", str(other.pk), stdout=StringIO())
    assert list(AudienceSignalRun.objects.values_list("account_id", flat=True)) == [
        other.pk
    ]
