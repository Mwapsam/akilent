"""A guided WhatsApp menu, end to end: no AI, no free-text understanding.

"hello" -> buttons -> the customer taps -> the matching answer. Everything goes through the
real inbound webhook handler, so this pins the behaviour an owner relies on: a tap continues
the conversation the automation started, a typed answer works too, silence times out, and a
tap is never also treated as a fresh keyword message.
"""
import time
from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from apps.automation import api as automation_api
from apps.automation.models import WorkflowRun
from apps.automation.workflow_engine import DEFAULT_REPLY_TIMEOUT_SECONDS, run_due, validate_definition
from apps.contacts.models import Contact
from apps.whatsapp.models import OutboundMessage, WebhookEventLog
from apps.whatsapp.tasks import _handle_inbound_message
from apps.whatsapp.tests.test_auto_reply import WA_ID, Base

TRIGGER = "conversation.message_received"


def menu(**wait):
    """hello -> [Prices | Book | Talk] -> wait -> answer. ``wait`` overrides the wait step."""
    return {
        "trigger": {"type": TRIGGER, "match": {"mode": "starts_with", "any": ["hello"]}},
        "steps": [
            {"id": "ask", "type": "send_buttons", "text": "What do you need, {first_name}?",
             "buttons": [{"title": "Prices"}, {"title": "Book"}, {"title": "Talk to us"}], "next": "wait"},
            {"id": "wait", "type": "wait_for_reply", "timeout_seconds": 3600,
             "routes": {"prices": "say_prices", "book": "say_book"}, "on_timeout": "stop", **wait},
            {"id": "say_prices", "type": "reply_text", "text": "Prices start at K50.", "next": "tag"},
            {"id": "tag", "type": "add_tag", "tag": "asked-prices", "next": "stop"},
            {"id": "say_book", "type": "reply_text", "text": "Call us to book.", "next": "stop"},
            {"id": "say_other", "type": "reply_text", "text": "A person will reply soon.", "next": "stop"},
            {"id": "stop", "type": "stop"},
        ],
    }


def text(body):
    return {"type": "text", "text": {"body": body}}


def tap(option_id, title):
    return {"type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": option_id, "title": title}}}


class FlowBase(Base):
    def setUp(self):
        super().setUp()
        for target in ("apps.whatsapp.tasks._auto_reply_during_setup",):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)
        p = patch("apps.whatsapp.tasks._automation_events_enabled", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.seq = 0

    def publish(self, definition, slug="menu"):
        return automation_api.upsert_published_workflow(
            self.account, slug=slug, name=slug, definition=definition)

    def customer(self, message):
        self.seq += 1
        payload = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "PNID"}, "contacts": [{"profile": {"name": "Ada Mwape"}}],
            "messages": [{"from": WA_ID, "id": f"wamid.{self.seq}", "timestamp": str(int(time.time())), **message}],
        }}]}]}
        _handle_inbound_message(WebhookEventLog.objects.create(
            source="whatsapp", event_type="message", payload=payload))

    def sent(self):
        return [m.payload for m in OutboundMessage.objects.order_by("id")]

    def run_(self):
        return WorkflowRun.objects.order_by("id").last()



class MenuFlowTest(FlowBase):
    # -- the happy path -----------------------------------------------------------------------

    def test_hello_offers_the_buttons_and_waits_for_a_choice(self):
        self.publish(menu())
        self.customer(text("Hello there"))
        [offer] = self.sent()
        self.assertEqual(offer["type"], "interactive")
        self.assertEqual(offer["body"], "What do you need, Ada Mwape?")
        self.assertEqual(offer["options"], ["Prices", "Book", "Talk to us"])
        self.assertEqual(self.run_().status, WorkflowRun.Status.WAITING)
        self.assertEqual(self.run_().current_step, "wait")

    def test_a_tap_continues_the_run_and_takes_the_matching_route(self):
        self.publish(menu())
        self.customer(text("Hello"))
        self.customer(tap("prices", "Prices"))
        self.assertEqual([p.get("body") for p in self.sent()], ["What do you need, Ada Mwape?", "Prices start at K50."])
        run = self.run_()
        self.assertEqual(run.status, WorkflowRun.Status.COMPLETED)
        self.assertEqual(run.context["reply"]["id"], "prices")
        self.assertEqual(list(Contact.objects.get().tags.values_list("name", flat=True)), ["asked-prices"])

    def test_the_other_button_takes_its_own_route(self):
        self.publish(menu())
        self.customer(text("Hello"))
        self.customer(tap("book", "Book"))
        self.assertEqual(self.sent()[-1]["body"], "Call us to book.")
        self.assertFalse(Contact.objects.get().tags.exists())

    def test_a_typed_answer_that_names_a_choice_works_like_a_tap(self):
        self.publish(menu())
        self.customer(text("Hello"))
        self.customer(text("Book!"))
        self.assertEqual(self.sent()[-1]["body"], "Call us to book.")

    # -- what is not a reply --------------------------------------------------------------------

    def test_an_unrelated_message_is_not_swallowed_and_the_menu_keeps_waiting(self):
        self.publish(menu())
        self.publish({"trigger": {"type": TRIGGER, "match": {"any": ["opening hours"]}},
                      "steps": [{"id": "r", "type": "reply_text", "text": "9 to 5.", "next": "stop"},
                                {"id": "stop", "type": "stop"}]}, slug="hours")
        self.customer(text("Hello"))
        self.customer(text("What are your opening hours?"))
        self.assertEqual(self.sent()[-1]["body"], "9 to 5.")
        waiting = WorkflowRun.objects.get(workflow__slug="menu")
        self.assertEqual(waiting.status, WorkflowRun.Status.WAITING)

    def test_a_reply_that_fits_no_route_goes_to_default_when_there_is_one(self):
        self.publish(menu(default="say_other"))
        self.customer(text("Hello"))
        self.customer(tap("talk", "Talk to us"))
        self.assertEqual(self.sent()[-1]["body"], "A person will reply soon.")

    def test_a_tap_answering_the_menu_does_not_also_fire_a_keyword_workflow(self):
        """The customer tapped "Prices". A separate rule listening for the word "prices"
        must not answer as well, or they get two replies to one tap."""
        self.publish(menu())
        self.publish({"trigger": {"type": TRIGGER, "match": {"any": ["prices"]}},
                      "steps": [{"id": "r", "type": "reply_text", "text": "KEYWORD ANSWER", "next": "stop"},
                                {"id": "stop", "type": "stop"}]}, slug="keyword")
        self.customer(text("Hello"))
        self.customer(tap("prices", "Prices"))
        bodies = [p.get("body") for p in self.sent()]
        self.assertIn("Prices start at K50.", bodies)
        self.assertNotIn("KEYWORD ANSWER", bodies)

    def test_a_tap_with_no_waiting_menu_is_an_ordinary_message(self):
        self.publish({"trigger": {"type": TRIGGER, "match": {"any": ["prices"]}},
                      "steps": [{"id": "r", "type": "reply_text", "text": "KEYWORD ANSWER", "next": "stop"},
                                {"id": "stop", "type": "stop"}]}, slug="keyword")
        self.customer(tap("prices", "Prices"))
        self.assertEqual([p["body"] for p in self.sent()], ["KEYWORD ANSWER"])

    # -- silence ------------------------------------------------------------------------------------

    def test_no_answer_times_out_and_a_late_tap_no_longer_resumes_it(self):
        self.publish(menu())
        self.customer(text("Hello"))
        WorkflowRun.objects.update(next_due_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(run_due(), 1)
        self.assertEqual(self.run_().status, WorkflowRun.Status.COMPLETED)
        before = len(self.sent())
        self.customer(tap("prices", "Prices"))
        self.assertEqual(len(self.sent()), before)          # nothing was waiting for it

    def test_the_timeout_can_take_a_fallback_route(self):
        self.publish(menu(on_timeout="say_other"))
        self.customer(text("Hello"))
        WorkflowRun.objects.update(next_due_at=timezone.now() - timedelta(seconds=1))
        run_due()
        self.assertEqual(self.sent()[-1]["body"], "A person will reply soon.")

    def test_a_repeated_hello_does_not_start_a_second_menu_inside_the_cooldown(self):
        self.publish(menu())
        self.customer(text("Hello"))
        self.customer(text("Hello again"))
        self.assertEqual(len(self.sent()), 1)

    # -- lists ------------------------------------------------------------------------------------------

    def test_a_list_can_be_offered_and_a_choice_routed(self):
        self.publish({
            "trigger": {"type": TRIGGER, "match": {"any": ["menu"]}},
            "steps": [
                {"id": "ask", "type": "send_list", "text": "Pick a product", "button": "See products",
                 "rows": [{"title": "Sofa", "description": "3 seater"}, {"title": "Bed"}], "next": "wait"},
                {"id": "wait", "type": "wait_for_reply", "routes": {"sofa": "say"}, "on_timeout": "stop"},
                {"id": "say", "type": "reply_text", "text": "Sofas are K2000.", "next": "stop"},
                {"id": "stop", "type": "stop"},
            ]}, slug="list")
        self.customer(text("menu"))
        offer = self.sent()[0]["interactive"]
        self.assertEqual(offer["type"], "list")
        self.assertEqual(self.run_().current_step, "wait")
        self.customer({"type": "interactive", "interactive": {
            "type": "list_reply", "list_reply": {"id": "sofa", "title": "Sofa"}}})
        self.assertEqual(self.sent()[-1]["body"], "Sofas are K2000.")

    # -- routing a workflow on a specific tap -------------------------------------------------------------

    def test_a_workflow_can_start_from_one_particular_button(self):
        self.publish({"trigger": {"type": TRIGGER, "reply_id": ["prices"]},
                      "steps": [{"id": "r", "type": "reply_text", "text": "Here are the prices.", "next": "stop"},
                                {"id": "stop", "type": "stop"}]}, slug="from-button")
        self.customer(tap("book", "Book"))
        self.assertEqual(self.sent(), [])
        self.customer(tap("PRICES", "Prices"))                 # ids match ignoring case
        self.assertEqual(self.sent()[-1]["body"], "Here are the prices.")


# --- validation: a menu that could never work is refused at save time -----------------------------------------


def errors(definition):
    return [(e["field"], e["message"]) for e in validate_definition(definition) if e["severity"] == "error"]


def test_a_complete_menu_is_valid():
    assert errors(menu()) == []


def test_buttons_need_a_conversation_to_reply_in():
    definition = menu()
    definition["trigger"] = {"type": "contact.created"}
    assert any(field == "type" for field, _ in errors(definition))


def test_bad_buttons_are_caught_when_saving_not_when_a_customer_is_waiting():
    definition = menu()
    definition["steps"][0]["buttons"] = [{"title": "A"}, {"title": "B"}, {"title": "C"}, {"title": "D"}]
    assert any(field == "buttons" and "between 1 and 3" in msg for field, msg in errors(definition))


def test_a_wait_for_reply_needs_a_route_a_sensible_timeout_and_real_targets():
    definition = menu(routes={})
    assert any(field == "routes" for field, _ in errors(definition))
    assert any(field == "timeout_seconds" for field, _ in errors(menu(timeout_seconds=5)))
    assert any(field == "timeout_seconds" for field, _ in errors(menu(timeout_seconds=99999999)))
    assert any(field == "routes" for field, _ in errors(menu(routes={"prices": "nowhere"})))
    assert any(field == "default" for field, _ in errors(menu(default="nowhere")))
    assert any(field == "on_timeout" for field, _ in errors(menu(on_timeout="nowhere")))


def test_a_reply_id_trigger_filter_is_validated():
    ok = {"trigger": {"type": TRIGGER, "reply_id": "prices"},
          "steps": [{"id": "stop", "type": "stop"}]}
    assert errors(ok) == []
    for bad in ("", [], [""], 5):
        assert any(f == "trigger.reply_id" for f, _ in errors({**ok, "trigger": {"type": TRIGGER, "reply_id": bad}}))
    assert any(f == "trigger.reply_id" for f, _ in errors(
        {**ok, "trigger": {"type": "contact.created", "reply_id": "prices"}}))


def test_the_default_wait_is_a_day():
    assert DEFAULT_REPLY_TIMEOUT_SECONDS == 86400


# --- the one-click menu starter ---------------------------------------------------------------------------------

INSTALL = "/automations/starters/install/"


class MenuStarterTest(FlowBase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User

        from apps.accounts.models import Membership

        user = User.objects.create_user("owner", "owner@example.com", "pw")
        Membership.objects.create(user=user, account=self.account, role=Membership.Role.OWNER)
        self.client.force_login(user)

    def install(self, **fields):
        data = {"starter": "offer-a-menu", "menu_text": "Hi {first_name}, what do you need?",
                "opt_title_1": "Prices", "opt_reply_1": "Prices start at K50.",
                "opt_title_2": "Book a visit", "opt_reply_2": "Call us on 0971 234 567.", **fields}
        return self.client.post(INSTALL, data, follow=True)

    def workflow(self):
        from apps.automation.models import Workflow

        return Workflow.objects.filter(account=self.account, slug="offer-a-menu").first()

    def test_the_gallery_offers_the_menu_with_its_form(self):
        body = self.client.get("/automations/").content.decode()
        self.assertIn("Offer a menu when someone says hello", body)
        for field in ("menu_text", "opt_title_1", "opt_reply_3"):
            self.assertIn(f'name="{field}"', body)

    def test_installing_publishes_a_valid_menu(self):
        self.install()
        wf = self.workflow()
        self.assertEqual(wf.status, "published")
        self.assertEqual(errors(wf.definition), [])
        steps = {s["id"]: s for s in wf.definition["steps"]}
        self.assertEqual([b["title"] for b in steps["ask"]["buttons"]], ["Prices", "Book a visit"])
        self.assertEqual(steps["wait"]["routes"], {"prices": "answer_1", "book-a-visit": "answer_2"})
        self.assertEqual(steps["tag_2"]["tag"], "asked-book-a-visit")

    def test_the_installed_menu_works_end_to_end(self):
        self.install()
        self.customer(text("Hi"))
        self.assertEqual(self.sent()[0]["options"], ["Prices", "Book a visit"])
        self.customer(tap("book-a-visit", "Book a visit"))
        self.assertEqual(self.sent()[-1]["body"], "Call us on 0971 234 567.")
        self.assertEqual(list(Contact.objects.get().tags.values_list("name", flat=True)), ["asked-book-a-visit"])

    def test_an_option_needs_both_a_label_and_an_answer(self):
        resp = self.install(opt_reply_2="")
        self.assertIn("Option 2 needs both", resp.content.decode())
        self.assertIsNone(self.workflow())

    def test_at_least_one_option_is_required(self):
        resp = self.install(opt_title_1="", opt_reply_1="", opt_title_2="", opt_reply_2="")
        self.assertIn("Add at least one button", resp.content.decode())
        self.assertIsNone(self.workflow())

    def test_whatsapp_limits_are_explained_in_plain_words(self):
        self.assertIn("at most 20", self.install(opt_title_1="A" * 21).content.decode())
        self.assertIsNone(self.workflow())
        self.assertIn("share the name", self.install(opt_title_2="prices").content.decode())
        self.assertIsNone(self.workflow())

    def test_installing_again_updates_rather_than_duplicating(self):
        from apps.automation.models import Workflow

        self.install()
        self.install(opt_reply_1="New prices.")
        self.assertEqual(Workflow.objects.filter(account=self.account, slug="offer-a-menu").count(), 1)
        steps = {s["id"]: s for s in self.workflow().definition["steps"]}
        self.assertEqual(steps["answer_1"]["text"], "New prices.")
