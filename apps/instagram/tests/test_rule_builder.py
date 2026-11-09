"""Stage 5 rule builder, comment rules: each rule as a sentence, the editor in a drawer, and
the order set by drag or Move up / Move down instead of priority numbers."""

from django.test import TestCase

from apps.billing.models import AccountFeatureOverride
from apps.core import reorder
from apps.core.htmx import OVERLAY_TARGET
from apps.instagram.models import CommentTrigger, ModerationRule
from apps.instagram.models.moderation import RuleChannels
from apps.instagram.services.rule_text import sentence
from apps.instagram.tests.helpers import make_account, make_instagram_account

OVERLAY = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": OVERLAY_TARGET}


def _reply(account, name, priority, **kw):
    return CommentTrigger.objects.create(
        account=account,
        name=name,
        match_type=kw.pop("match_type", "keyword"),
        keywords=kw.pop("keywords", "price, how much"),
        reply_template="Thanks {username}!",
        priority=priority,
        **kw,
    )


class SentenceTest(TestCase):
    def setUp(self):
        self.account, _ = make_account()

    def test_reply_rule_reads_as_one_sentence(self):
        rule = _reply(self.account, "Price", 10, reply_type="dm")
        self.assertEqual(
            sentence(rule),
            "When a comment mentions “price” or “how much”, reply privately by DM.",
        )

    def test_channels_named_only_when_the_business_has_both(self):
        rule = _reply(self.account, "Price", 10, channels=RuleChannels.FACEBOOK)
        self.assertIn("a comment on Facebook", sentence(rule, show_channels=True))
        self.assertNotIn("Facebook", sentence(rule))

    def test_moderation_rule_says_what_it_does(self):
        rule = ModerationRule.objects.create(
            account=self.account,
            name="Spam",
            match_type=ModerationRule.MatchType.SPAM_DETECTION,
            moderation_action=ModerationRule.ModerationAction.HIDE,
            automation_trigger=ModerationRule.AutomationTrigger.NOTIFY_STAFF,
        )
        self.assertEqual(
            sentence(rule),
            "When a comment looks like spam, hide it and email your team.",
        )


class ReorderTest(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.a = _reply(self.account, "A", 5)
        self.b = _reply(self.account, "B", 5)
        self.c = _reply(self.account, "C", 70)

    def qs(self):
        return CommentTrigger.objects.filter(account=self.account)

    def names(self):
        return [r.name for r in self.qs().order_by("priority")]

    def test_renumber_breaks_ties_and_keeps_gaps(self):
        reorder.renumber(self.qs())
        self.assertEqual(
            list(self.qs().order_by("priority").values_list("priority", flat=True)),
            [10, 20, 30],
        )

    def test_move_up_and_at_the_edge(self):
        self.assertTrue(reorder.move(self.qs(), self.c.pk, "up"))
        self.assertEqual(self.names(), ["A", "C", "B"])
        self.assertFalse(reorder.move(self.qs(), self.a.pk, "up"))

    def test_another_accounts_rule_is_never_touched(self):
        other, _ = make_account()
        theirs = _reply(other, "Theirs", 1)
        reorder.renumber(self.qs(), [theirs.pk, self.c.pk])
        theirs.refresh_from_db()
        self.assertEqual(theirs.priority, 1)
        self.assertEqual(self.names(), ["C", "A", "B"])

    def test_a_new_rule_goes_last(self):
        self.assertEqual(reorder.next_priority(self.qs()), 80)


class RulesPageTest(TestCase):
    def setUp(self):
        self.account, self.user = make_account()
        make_instagram_account(self.account)
        AccountFeatureOverride.objects.create(
            account=self.account, key="instagram", grant=True, note="test"
        )
        self.client.force_login(self.user)

    def test_rows_show_sentences_and_move_buttons_in_run_order(self):
        _reply(self.account, "Second", 20)
        _reply(self.account, "First", 10, keywords="cost")
        page = self.client.get("/instagram/rules/").content.decode()
        self.assertLess(page.index("First"), page.index("Second"))
        self.assertIn("When a comment mentions “cost”, reply", page)
        self.assertIn('id="reply-rules" data-reorder', page)
        self.assertIn("data-reorder-form", page)
        self.assertIn('name="move" value="up"', page)
        self.assertIn('data-overlay-open="drawer"', page)
        self.assertNotIn("Lower runs first", page)

    def test_move_and_drag_post_the_new_order(self):
        first = _reply(self.account, "First", 10)
        second = _reply(self.account, "Second", 20)
        third = _reply(self.account, "Third", 30)
        response = self.client.post(
            "/instagram/rules/reply/reorder/", {"pk": third.pk, "move": "up"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.endswith("#reply-rules"))
        order = lambda: list(  # noqa: E731
            CommentTrigger.objects.order_by("priority").values_list("name", flat=True)
        )
        self.assertEqual(order(), ["First", "Third", "Second"])
        self.client.post(
            "/instagram/rules/reply/reorder/",
            {"order": [second.pk, first.pk, third.pk]},
        )
        self.assertEqual(order(), ["Second", "First", "Third"])

    def test_editor_opens_in_a_drawer_without_a_priority_field(self):
        rule = _reply(self.account, "Price", 10)
        html = self.client.get(
            f"/instagram/rules/reply/{rule.pk}/edit/", **OVERLAY
        ).content.decode()
        self.assertIn('data-overlay="drawer"', html)
        self.assertIn("Edit “Price”", html)
        self.assertNotIn('name="priority"', html)
        self.assertIn("keywordChips", html)  # keywords as chips

    def test_a_new_rule_runs_after_the_others_and_the_drawer_closes(self):
        _reply(self.account, "Existing", 40)
        response = self.client.post(
            "/instagram/rules/reply/new/",
            {
                "name": "New",
                "match_type": "keyword",
                "keywords": "price",
                "reply_type": "dm",
                "reply_template": "Hi",
            },
            **OVERLAY,
        )
        self.assertEqual(response.status_code, 204)
        self.assertIn("overlay-close", response["HX-Trigger"])
        self.assertEqual(CommentTrigger.objects.get(name="New").priority, 50)

    def test_editing_keeps_the_rules_place(self):
        rule = _reply(self.account, "Price", 10)
        _reply(self.account, "Later", 20)
        self.client.post(
            f"/instagram/rules/reply/{rule.pk}/edit/",
            {
                "name": "Price!",
                "match_type": "keyword",
                "keywords": "price",
                "reply_type": "dm",
                "reply_template": "Hi",
            },
        )
        rule.refresh_from_db()
        self.assertEqual((rule.name, rule.priority), ("Price!", 10))
