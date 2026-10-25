"""0025 clears the post and insight credit that guesses were given before 2026-10-25, and
0026 then makes the database refuse it. Explicit attributions keep their credit."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

_APP = "conversations"
_BEFORE = "0024_automation_pause"
_AFTER = "0026_guess_credits_nothing"


@pytest.mark.django_db(transaction=True)
def test_old_guesses_lose_their_credit_and_explicit_rows_keep_it():
    executor = MigrationExecutor(connection)
    if (_APP, _BEFORE) not in executor.loader.graph.nodes:
        pytest.skip("migration graph unavailable (--nomigrations)")
    executor.migrate([(_APP, _BEFORE)])
    executor.loader.build_graph()
    old = executor.loader.project_state([(_APP, _BEFORE)]).apps

    account = old.get_model("accounts", "Account").objects.create(company_name="Old")
    contact = old.get_model("contacts", "Contact").objects.create(
        account=account, phone="+260971234567"
    )
    conversation = old.get_model(_APP, "Conversation").objects.create(
        account=account, contact=contact, channel="whatsapp", public_id="conv_old"
    )
    post = old.get_model("publishing", "ContentPost").objects.create(
        account=account, platforms=["facebook"], media_type="text", public_id="post_old"
    )
    variant = old.get_model("publishing", "ContentPostVariant").objects.create(
        post=post, platform="facebook"
    )
    entry = old.get_model(_APP, "ConversationEntryPoint").objects.create(
        account=account,
        conversation=conversation,
        kind="tracked_link",
        post=post,
        variant=variant,
    )
    Order = old.get_model("commerce", "Order")
    Attribution = old.get_model(_APP, "ConversationAttribution")
    rows = {}
    for method, public_id in (
        ("recent_conversation", "ord_guess"),
        ("explicit", "ord_named"),
    ):
        order = Order.objects.create(
            account=account,
            contact=contact,
            conversation=conversation,
            public_id=public_id,
            total=10,
            currency="ZMW",
            status="paid",
            paid_at=timezone.now(),
        )
        rows[method] = Attribution.objects.create(
            account=account,
            conversation=conversation,
            channel="whatsapp",
            method=method,
            order=order,
            entry_point=entry,
            source_post=post,
            source_variant=variant,
        ).pk

    executor = MigrationExecutor(connection)
    executor.migrate([(_APP, _AFTER)])
    new = executor.loader.project_state([(_APP, _AFTER)]).apps
    NewAttribution = new.get_model(_APP, "ConversationAttribution")

    guess = NewAttribution.objects.get(pk=rows["recent_conversation"])
    assert guess.method == "recent_conversation"
    assert guess.conversation_id == conversation.pk  # the guess itself stays on record
    assert (
        guess.source_post_id,
        guess.source_variant_id,
        guess.entry_point_id,
        guess.originated_from_id,
    ) == (None, None, None, None)
    named = NewAttribution.objects.get(pk=rows["explicit"])
    assert (named.source_post_id, named.entry_point_id) == (post.pk, entry.pk)

    # Leave the test database at the latest state for whatever runs next.
    MigrationExecutor(connection).migrate(
        MigrationExecutor(connection).loader.graph.leaf_nodes()
    )
