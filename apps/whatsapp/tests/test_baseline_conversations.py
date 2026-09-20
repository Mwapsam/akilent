"""Step 0a baseline: definitions, invariants, account isolation, read-only, contract."""
import copy
import hashlib
import json
import tempfile
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Account
from apps.commerce.models import Order
from apps.contacts.models import Contact
from apps.crm.models import Lead
from apps.whatsapp.baseline import build_snapshot, measure_account, sum_results
from apps.whatsapp.baseline_schema import (
    SCHEMA_VERSION,
    SnapshotSchemaError,
    validate_snapshot,
)
from apps.whatsapp.models import Conversation, MessageLog, WhatsAppContact

IN, OUT = MessageLog.Direction.INBOUND, MessageLog.Direction.OUTBOUND
AS_OF = timezone.now()
BASE = AS_OF - timedelta(days=10)  # far outside the grace period
M, H = 60, 3600
_seq = iter(range(1, 10_000))


def make_account(slug):
    return Account.objects.create(company_name=slug.upper(), slug=slug)


def make_wa(account, with_contact=False):
    n = next(_seq)
    phone = f"+26097{1000000 + n}"
    contact = Contact.objects.create(account=account, phone=phone) if with_contact else None
    wa = WhatsAppContact.objects.create(account=account, phone_number=phone, contact=contact)
    return wa, Conversation.objects.create(account=account, contact=wa)


def add(account, wa, convo, direction, ts, status="delivered", mtype="text", content="", created_at=None):
    m = MessageLog.objects.create(
        account=account, conversation=convo, contact=wa, direction=direction, timestamp=ts,
        status=status, message_type=mtype, content=content, message_id=uuid.uuid4().hex,
    )
    MessageLog.objects.filter(pk=m.pk).update(created_at=created_at or ts)
    return m


def enquiry(account, replies=(), start=BASE, with_contact=False, content="Hi"):
    """One inbound at ``start``; replies = [(offset_seconds, status, message_type)]."""
    wa, convo = make_wa(account, with_contact)
    add(account, wa, convo, IN, start, content=content)
    for offset, status, mtype in replies:
        add(account, wa, convo, OUT, start + timedelta(seconds=offset), status=status, mtype=mtype)
    return wa, convo


def measure(account, gaps=(24,), grace=1, since=None, until=None):
    return measure_account(account, list(gaps), grace, since, until, AS_OF)


def first(results):
    return results[0]


class DefinitionsTest(TestCase):
    def setUp(self):
        self.a = make_account("a")

    def test_response_buckets_are_exclusive_with_correct_boundaries(self):
        # gap 72h so the 30h reply stays inside the exchange.
        for offset in (2 * M, 5 * M, 59 * M + 59, 1 * H, 30 * H):
            enquiry(self.a, [(offset, "delivered", "text")])
        r = first(measure(self.a, gaps=(72,)))
        self.assertEqual(r["response_time"]["lt_5m"], 1)        # 2m
        self.assertEqual(r["response_time"]["5m_to_1h"], 2)     # exactly 5m, 59m59s
        self.assertEqual(r["response_time"]["1h_to_24h"], 1)    # exactly 1h
        self.assertEqual(r["response_time"]["gte_24h"], 1)      # 30h
        self.assertEqual(r["enquiries"]["answered"], 5)

    def test_percentiles_null_below_five_and_present_from_five(self):
        for offset in (60, 120, 180, 240):
            enquiry(self.a, [(offset, "sent", "text")])
        rt = first(measure(self.a))["response_time"]
        self.assertIsNone(rt["median_seconds"])
        self.assertIsNone(rt["p90_seconds"])
        enquiry(self.a, [(300 - 1, "sent", "text")])
        rt = first(measure(self.a))["response_time"]
        self.assertEqual(rt["median_seconds"], 180)
        self.assertEqual(rt["p90_seconds"], 299)

    def test_unanswered_vs_never_answered(self):
        enquiry(self.a, [(60, "delivered", "text")])  # keeps the account's outbound logging present
        enquiry(self.a)                                # nobody ever replies
        # Replied only 30h later: with a 24h gap that reply is a later exchange.
        enquiry(self.a, [(30 * H, "delivered", "text")])
        e = first(measure(self.a, gaps=(24,)))["enquiries"]
        self.assertEqual((e["answered"], e["unanswered"], e["never_answered"]), (1, 2, 1))
        # ...and answered when the gap is 72h: sensitivity to the gap is visible.
        e72 = first(measure(self.a, gaps=(72,)))["enquiries"]
        self.assertEqual((e72["answered"], e72["unanswered"], e72["never_answered"]), (2, 1, 1))

    def test_queued_or_failed_only_is_indeterminate_not_forced(self):
        enquiry(self.a, [(60, "delivered", "text")])
        enquiry(self.a, [(60, "failed", "text")])
        enquiry(self.a, [(60, "queued", "text")])
        e = first(measure(self.a))["enquiries"]
        self.assertEqual((e["answered"], e["indeterminate"]), (1, 2))
        self.assertEqual(e["indeterminate_reasons"], {"outbound_not_delivered": 2})

    def test_outbound_logged_before_the_enquiry_was_received_is_indeterminate(self):
        wa, convo = make_wa(self.a)
        add(self.a, wa, convo, IN, BASE, created_at=BASE)
        add(self.a, wa, convo, OUT, BASE + timedelta(minutes=1),
            created_at=BASE - timedelta(hours=1))  # pre-queued, clocks disagree
        e = first(measure(self.a))["enquiries"]
        self.assertEqual(e["indeterminate_reasons"], {"outbound_created_before_enquiry": 1})
        self.assertEqual(e["answered"], 0)

    def test_within_grace_period_is_indeterminate(self):
        enquiry(self.a, [(60, "delivered", "text")])
        enquiry(self.a, start=AS_OF - timedelta(minutes=10))
        e = first(measure(self.a, grace=1))["enquiries"]
        self.assertEqual(e["indeterminate_reasons"], {"within_grace_period": 1})
        self.assertEqual(e["unanswered"], 0)

    def test_account_with_no_outbound_at_all_is_indeterminate(self):
        enquiry(self.a)
        e = first(measure(self.a))["enquiries"]
        self.assertEqual(e["indeterminate_reasons"], {"no_outbound_logged_for_account": 1})
        self.assertEqual((e["answered"], e["unanswered"]), (0, 0))

    def test_exchanges_split_by_gap(self):
        wa, convo = make_wa(self.a)
        add(self.a, wa, convo, IN, BASE)
        add(self.a, wa, convo, OUT, BASE + timedelta(minutes=1))
        add(self.a, wa, convo, IN, BASE + timedelta(hours=30))
        add(self.a, wa, convo, OUT, BASE + timedelta(hours=30, minutes=2))
        self.assertEqual(first(measure(self.a, gaps=(24,)))["enquiries"]["inbound"], 2)
        self.assertEqual(first(measure(self.a, gaps=(72,)))["enquiries"]["inbound"], 1)

    def test_stop_start_and_business_initiated_exchanges_are_not_enquiries(self):
        enquiry(self.a, [(60, "delivered", "text")])
        enquiry(self.a, content="STOP")
        enquiry(self.a, content=" start. ")
        wa, convo = make_wa(self.a)  # business speaks first, customer replies later
        add(self.a, wa, convo, OUT, BASE, mtype="template")
        add(self.a, wa, convo, IN, BASE + timedelta(hours=2))
        self.assertEqual(first(measure(self.a))["enquiries"]["inbound"], 1)

    def test_template_reply_counts_as_answered_and_is_flagged_in_data_quality(self):
        enquiry(self.a, [(60, "sent", "template")])
        r = first(measure(self.a))
        self.assertEqual(r["enquiries"]["answered"], 1)
        self.assertEqual(r["data_quality"]["outbound_template"], 1)

    def test_since_until_window_selects_enquiries_by_start(self):
        enquiry(self.a, [(60, "sent", "text")], start=BASE)
        enquiry(self.a, [(60, "sent", "text")], start=BASE + timedelta(days=3))
        r = first(measure(self.a, since=BASE + timedelta(days=1), until=BASE + timedelta(days=5)))
        self.assertEqual(r["enquiries"]["inbound"], 1)

    def test_conversion_cohort_counts_only_activity_after_the_enquiry(self):
        wa1, _ = enquiry(self.a, [(60, "sent", "text")], with_contact=True)
        wa2, _ = enquiry(self.a, [(60, "sent", "text")], with_contact=True)
        wa3, _ = enquiry(self.a, [(60, "sent", "text")], with_contact=True)
        after, before = BASE + timedelta(days=1), BASE - timedelta(days=1)
        lead = Lead.objects.create(account=self.a, contact=wa1.contact)
        Lead.objects.filter(pk=lead.pk).update(created_at=after)
        old = Lead.objects.create(account=self.a, contact=wa2.contact)
        Lead.objects.filter(pk=old.pk).update(created_at=before)  # predates the enquiry
        order = Order.objects.create(account=self.a, contact=wa1.contact)
        Order.objects.filter(pk=order.pk).update(created_at=after)
        c = first(measure(self.a))["conversion_cohort"]
        self.assertEqual(c, {"enquiries_with_lead": 1, "enquiries_with_deal": 0, "enquiries_with_order": 1})


class IsolationTest(TestCase):
    def setUp(self):
        self.a, self.b = make_account("a"), make_account("b")
        # Account A: 2 enquiries, 1 answered.
        enquiry(self.a, [(60, "delivered", "text")], with_contact=True)
        enquiry(self.a, with_contact=True)
        # Account B: 5 enquiries, 4 answered.
        for _ in range(4):
            enquiry(self.b, [(120, "delivered", "text")], with_contact=True)
        wa_b, _ = enquiry(self.b, with_contact=True)
        # A lead in B must never show up in A's cohort (and vice versa).
        lead = Lead.objects.create(account=self.b, contact=wa_b.contact)
        Lead.objects.filter(pk=lead.pk).update(created_at=BASE + timedelta(days=1))

    def snapshot(self, accounts, total=False):
        return build_snapshot(accounts, [24], 1, None, None, None, None, AS_OF, "test", include_total=total)

    def test_each_account_is_measured_independently(self):
        snap = self.snapshot([self.a, self.b], total=True)
        by_slug = {e["account"]["slug"]: e["results"][0] for e in snap["accounts"]}
        self.assertEqual((by_slug["a"]["enquiries"]["inbound"], by_slug["a"]["enquiries"]["answered"]), (2, 1))
        self.assertEqual((by_slug["b"]["enquiries"]["inbound"], by_slug["b"]["enquiries"]["answered"]), (5, 4))
        self.assertEqual(by_slug["a"]["conversion_cohort"]["enquiries_with_lead"], 0)
        self.assertEqual(by_slug["b"]["conversion_cohort"]["enquiries_with_lead"], 1)
        total = snap["total"]["results"][0]["enquiries"]
        self.assertEqual((total["inbound"], total["answered"]), (7, 5))
        validate_snapshot(snap)

    def test_single_account_run_returns_only_that_account(self):
        snap = self.snapshot([self.a])
        self.assertEqual([e["account"]["slug"] for e in snap["accounts"]], ["a"])
        self.assertEqual(snap["accounts"][0]["results"][0]["enquiries"]["inbound"], 2)

    def test_total_is_the_sum_of_partitioned_results(self):
        per_account = [measure(self.a), measure(self.b)]
        total = sum_results(per_account)[0]["enquiries"]
        self.assertEqual(total["inbound"], sum(r[0]["enquiries"]["inbound"] for r in per_account))

    def test_no_intermediate_unscoped_messagelog_query(self):
        with CaptureQueriesContext(connection) as ctx:
            self.snapshot([self.a, self.b], total=True)
        sql = [q["sql"] for q in ctx.captured_queries if "whatsapp_messagelog" in q["sql"]]
        self.assertTrue(sql)
        for statement in sql:
            self.assertIn('"account_id" =', statement)


class SafetyAndContractTest(TestCase):
    def setUp(self):
        self.a = make_account("a")
        for offset in (60, 120, 180, 240, 300):
            enquiry(self.a, [(offset, "delivered", "text")], with_contact=True)
        enquiry(self.a, with_contact=True)

    def snapshot(self):
        return build_snapshot([self.a], [24, 72], 1, None, None, None, None, AS_OF, "test")

    def test_read_only_no_writes_and_no_row_changes(self):
        counts = (MessageLog.objects.count(), Lead.objects.count(), Order.objects.count())
        with CaptureQueriesContext(connection) as ctx:
            self.snapshot()
        for q in ctx.captured_queries:
            self.assertNotRegex(q["sql"].lstrip().upper(), r"^(INSERT|UPDATE|DELETE|SAVEPOINT|REPLACE)")
        self.assertEqual(counts, (MessageLog.objects.count(), Lead.objects.count(), Order.objects.count()))

    def test_does_not_read_the_conversations_message_table(self):
        with CaptureQueriesContext(connection) as ctx:
            self.snapshot()
        self.assertFalse([q for q in ctx.captured_queries if "conversations_message" in q["sql"]])

    def test_idempotent(self):
        self.assertEqual(self.snapshot(), self.snapshot())

    def test_snapshot_satisfies_the_contract_and_invariants(self):
        snap = self.snapshot()
        validate_snapshot(snap)
        self.assertEqual(snap["schema_version"], SCHEMA_VERSION)
        self.assertEqual(snap["metadata"]["business_hours_adjustment"], "none")
        self.assertEqual({r["enquiry_gap_hours"] for r in snap["accounts"][0]["results"]}, {24, 72})
        for r in snap["accounts"][0]["results"]:
            e, rt = r["enquiries"], r["response_time"]
            self.assertEqual(e["answered"] + e["unanswered"] + e["indeterminate"], e["inbound"])
            self.assertLessEqual(e["never_answered"], e["unanswered"])
            self.assertEqual(sum(rt[k] for k in ("lt_5m", "5m_to_1h", "1h_to_24h", "gte_24h")), e["answered"])

    def test_contract_rejects_drift(self):
        snap = self.snapshot()

        extra = copy.deepcopy(snap)
        extra["accounts"][0]["results"][0]["enquiries"]["surprise"] = 1
        with self.assertRaises(SnapshotSchemaError):
            validate_snapshot(extra)

        renamed = copy.deepcopy(snap)
        rt = renamed["accounts"][0]["results"][0]["response_time"]
        rt["lt_1h"] = rt.pop("5m_to_1h")
        with self.assertRaises(SnapshotSchemaError):
            validate_snapshot(renamed)

        broken = copy.deepcopy(snap)
        broken["accounts"][0]["results"][0]["enquiries"]["answered"] += 1
        with self.assertRaises(SnapshotSchemaError):
            validate_snapshot(broken)

        wrong_version = copy.deepcopy(snap)
        wrong_version["schema_version"] = "9.9"
        with self.assertRaises(SnapshotSchemaError):
            validate_snapshot(wrong_version)

        fake_precision = copy.deepcopy(snap)
        fake_precision["accounts"][0]["results"][0]["response_time"]["median_seconds"] = 1.0
        fake_precision["accounts"][0]["results"][0]["enquiries"].update(answered=1, indeterminate=0)
        with self.assertRaises(SnapshotSchemaError):
            validate_snapshot(fake_precision)


class CommandTest(TestCase):
    def setUp(self):
        self.a, self.b = make_account("a"), make_account("b")
        enquiry(self.a, [(60, "delivered", "text")])
        enquiry(self.b, [(60, "delivered", "text")])
        enquiry(self.b)
        self.tmp = Path(tempfile.mkdtemp())

    def run_cmd(self, *args):
        out = StringIO()
        call_command("baseline_conversations", *args, stdout=out)
        return out.getvalue()

    def test_account_selector_by_slug_and_id(self):
        self.assertIn("Account a", self.run_cmd("--account", "a"))
        self.assertIn("Account a", self.run_cmd("--account", str(self.a.id)))
        with self.assertRaises(CommandError):
            self.run_cmd("--account", "nope")

    def test_report_states_gap_grace_limitations_and_non_attribution(self):
        report = self.run_cmd("--account", "a")
        for text in ("Enquiry gap 24h", "Enquiry gap 72h", "Sensitivity", "grace 1", "NOT attribution",
                     "Business-hours and timezone adjustments are not applied", "reconciliation"):
            self.assertIn(text, report)

    def test_all_accounts_total_requires_all_accounts(self):
        with self.assertRaises(CommandError):
            self.run_cmd("--account", "a", "--all-accounts-total")
        report = self.run_cmd("--all-accounts", "--all-accounts-total")
        self.assertIn("TOTAL", report)

    def test_out_writes_once_with_hash_record_and_never_overwrites(self):
        path = self.tmp / "before.json"
        with self.assertRaises(CommandError):  # operator required
            self.run_cmd("--all-accounts", "--out", str(path))
        self.run_cmd("--all-accounts", "--all-accounts-total", "--out", str(path), "--operator", "Sam")
        snap = json.loads(path.read_text())
        validate_snapshot(snap)
        record = json.loads((self.tmp / "before.json.measurement.json").read_text())
        self.assertEqual(record["output_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(record["operator"], "Sam")
        self.assertEqual(set(record), {"baseline_id", "generated_at", "git_commit",
                                       "command_arguments", "output_sha256", "operator"})
        original = path.read_bytes()
        with self.assertRaises(CommandError):  # immutable
            self.run_cmd("--all-accounts", "--out", str(path), "--operator", "Sam")
        self.assertEqual(path.read_bytes(), original)

    def test_snapshot_contains_no_message_content_or_phone_numbers(self):
        path = self.tmp / "agg.json"
        self.run_cmd("--all-accounts", "--out", str(path), "--operator", "Sam")
        text = path.read_text()
        self.assertNotIn("+26097", text)
        self.assertNotIn('"Hi"', text)

    def test_argument_validation(self):
        for bad in (["--gap-hours", "0"], ["--gap-hours", "24,24"], ["--gap-hours", "x"],
                    ["--since", "2026-13-01"], ["--grace-hours", "-1"],
                    ["--since", "2026-05-02", "--until", "2026-05-01"]):
            with self.assertRaises(CommandError, msg=bad):
                self.run_cmd("--account", "a", *bad)
