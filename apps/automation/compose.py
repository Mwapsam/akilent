"""Turn a plan made of fixed blocks into a real workflow: AI names the blocks, this builds.

When no single ready-made intent (``intents.INTENTS``) fits what an owner described, the AI may
answer with an ordered list of blocks from the grammar below. Only these blocks and their listed
fields exist; anything else is dropped and reported. The definition, step ids, routing and the
"why I built it this way" reasons are all made here, deterministically, and checked by
``validate_definition`` like any other automation. Nothing goes live without the owner's review.

Plan shape::

    {
        "name": "...",
        "trigger": {"block": "when_message", "keywords": ["price"]},
        "steps": [
            {"block": "reply", "text": "..."},
            {"block": "if_tag", "tag": "vip", "then": [...], "else": [...]},
        ],
    }

Works without AI too: anything that can write this shape (tests, a future template gallery)
gets the same checks.
"""

from __future__ import annotations

from typing import Any

from django.utils.text import slugify

MAX_STEPS = 25

# Trigger blocks -> (trigger definition builder, sentence)
TRIGGER_BLOCKS = {
    "when_message": "a customer messages you",
    "when_closed": "a customer messages you while you're closed",
    "when_new_customer": "a new customer is added",
    "when_interested": "someone becomes an interested customer",
    "when_lead_status": "an interested customer's status changes",
    "when_order_paid": "an order is paid",
    "when_form_completed": "a customer finishes a form",
    "manual": "you run it for a customer",
}

# Step blocks and the fields each accepts.
STEP_BLOCKS: dict[str, set[str]] = {
    "reply": {"text"},
    "ask": {"question", "save_as", "on"},
    "buttons": {"text", "options"},
    "wait": {"amount", "unit"},
    "if_tag": {"tag", "then", "else"},
    "if_open": {"then", "else"},
    "tag": {"tag"},
    "untag": {"tag"},
    "track_interest": set(),
    "mark_status": {"status"},
    "hand_to_team": {"to", "message"},
    "notify_team": {"text"},
    "follow_up": {"in_hours", "note"},
    "send_form": {"form"},
    "classify": {"labels", "examples"},
}

GRAMMAR = """Triggers (exactly one):
- {"block": "when_message", "keywords": ["<optional words the message mentions>"]}
- {"block": "when_closed"}  (a message outside opening hours)
- {"block": "when_new_customer"} | {"block": "when_interested"} | {"block": "when_order_paid"}
- {"block": "when_lead_status", "status": "qualified" | "lost" | "any"}
- {"block": "when_form_completed", "form": "<form name>"}
Steps (in order, at most 12):
- {"block": "reply", "text": "<message>"}
- {"block": "ask", "question": "<question>", "save_as": "<short field name>", "on": "customer" | "lead"}
- {"block": "buttons", "text": "<question>", "options": [{"title": "<max 20 chars>", "reply": "<answer>"}]}
- {"block": "wait", "amount": <number>, "unit": "minutes" | "hours" | "days"}
- {"block": "if_tag", "tag": "<tag>", "then": [<steps>], "else": [<steps>]}
- {"block": "if_open", "then": [<steps>], "else": [<steps>]}  (open = within opening hours)
- {"block": "tag", "tag": "<tag>"} | {"block": "untag", "tag": "<tag>"}
- {"block": "track_interest"} | {"block": "mark_status", "status": "contacted" | "qualified" | "lost"}
- {"block": "hand_to_team", "to": "least_busy" | "<teammate email>", "message": "<what to tell them>"}
- {"block": "notify_team", "text": "<email to the owners>"}
- {"block": "follow_up", "in_hours": <number>, "note": "<what to check>"}
- {"block": "send_form", "form": "<one of the business's form names>"}
- {"block": "classify", "labels": ["<2-8 short labels>"], "examples": {"<label>": ["<example message>"]}}"""

_UNITS = {
    "minutes": 60,
    "minute": 60,
    "hours": 3600,
    "hour": 3600,
    "days": 86400,
    "day": 86400,
}


class _Builder:
    def __init__(self, account):
        self.account = account
        self.steps: list[dict] = []
        self.reasons: list[str] = []
        self.dropped: list[str] = []
        self.needs: list[str] = []
        self.texts: list[str] = []
        self.counter = 0

    def new_id(self, kind: str) -> str:
        self.counter += 1
        return f"{kind.replace('_', '-')}-{self.counter}"

    def add(self, step: dict) -> dict:
        self.steps.append(step)
        return step

    # Each step block compiles to one or more steps; returns (first_id, loose ends): the
    # (step, field) pairs that should point at whatever comes after.
    def compile_seq(self, blocks: list) -> tuple[str, list[tuple[dict, str]]]:
        first: str = ""
        tails: list[tuple[dict, str]] = []
        for block in blocks or []:
            if len(self.steps) >= MAX_STEPS:
                self.dropped.append("some steps (an automation can have at most 25)")
                break
            head, new_tails = self.compile_block(block)
            if not head:
                continue
            for t in tails:
                self._point(t, head)
            first = first or head
            tails = new_tails
        return first, tails

    @staticmethod
    def _point(tail: tuple[dict, str], target: str) -> None:
        step, field = tail
        step[field] = target

    def compile_block(self, block) -> tuple[str, list[tuple[dict, str]]]:
        if not isinstance(block, dict):
            self.dropped.append("a step that wasn't understood")
            return "", []
        kind = block.get("block")
        if kind not in STEP_BLOCKS:
            self.dropped.append(f"a “{kind}” step (not something automations can do)")
            return "", []
        extra = set(block) - STEP_BLOCKS[kind] - {"block"}
        if extra:
            self.dropped.append(f"{', '.join(sorted(extra))} on a “{kind}” step")
        return getattr(self, f"_b_{kind}")(block)

    # ── blocks ──
    def _text(self, value, limit=1000) -> str:
        text = " ".join(str(value or "").split())[:limit]
        if text:
            self.texts.append(text)
        return text

    def _b_reply(self, b):
        text = self._text(b.get("text"))
        if not text:
            self.dropped.append("a reply with no words")
            return "", []
        s = self.add(
            {"id": self.new_id("reply"), "type": "reply_text", "text": text, "next": ""}
        )
        return s["id"], [(s, "next")]

    def _b_ask(self, b):
        question = self._text(b.get("question"), 1024)
        key = slugify(str(b.get("save_as") or "")).replace("-", "_")[:40]
        if not question or not key:
            self.dropped.append("a question without its wording or where to save it")
            return "", []
        target = "lead" if b.get("on") == "lead" else "contact"
        self._ensure_field(target, key)
        s = self.add(
            {
                "id": self.new_id("ask"),
                "type": "ask_question",
                "question": question,
                "attribute": key,
                "target": target,
                "max_attempts": 2,
                "timeout_seconds": 86400,
                "next": "",
                "on_timeout": "",
                "on_invalid": "",
            }
        )
        self.reasons.append(f"Saves their answer to “{question[:40]}” as {key}.")
        return s["id"], [(s, "next")]

    def _ensure_field(self, entity: str, key: str) -> None:
        from apps.contacts.models import CustomAttributeDef

        if not CustomAttributeDef.objects.filter(
            account=self.account, entity=entity, key=key
        ).exists():
            self.needs.append(
                f"A {'lead' if entity == 'lead' else 'customer'} field called “{key}” will be "
                "created when you save this."
            )

    def _b_buttons(self, b):
        text = self._text(b.get("text"), 1024)
        options = [
            o
            for o in (b.get("options") or [])
            if isinstance(o, dict) and str(o.get("title") or "").strip()
        ][:3]
        if not text or not options:
            self.dropped.append("buttons without a question or choices")
            return "", []
        ask = self.add(
            {
                "id": self.new_id("buttons"),
                "type": "send_buttons",
                "text": text,
                "buttons": [{"title": str(o["title"]).strip()[:20]} for o in options],
                "next": "",
            }
        )
        wait = self.add(
            {
                "id": self.new_id("wait-for-reply"),
                "type": "wait_for_reply",
                "routes": {},
                "timeout_seconds": 86400,
            }
        )
        ask["next"] = wait["id"]
        tails = []
        for o in options:
            title = str(o["title"]).strip()[:20]
            reply = self._text(o.get("reply"))
            if reply:
                r = self.add(
                    {
                        "id": self.new_id("reply"),
                        "type": "reply_text",
                        "text": reply,
                        "next": "",
                    }
                )
                wait["routes"][title] = r["id"]
                tails.append((r, "next"))
        tails.append((wait, "on_timeout"))
        self.reasons.append(
            "Offers "
            + ", ".join(str(o["title"]).strip()[:20] for o in options)
            + " as buttons."
        )
        return ask["id"], tails

    def _b_wait(self, b):
        try:
            amount = max(1, int(float(b.get("amount") or 1)))
        except (TypeError, ValueError):
            amount = 1
        unit = _UNITS.get(str(b.get("unit") or "days").lower(), 86400)
        seconds = min(amount * unit, 30 * 86400)
        s = self.add(
            {"id": self.new_id("wait"), "type": "wait", "seconds": seconds, "next": ""}
        )
        return s["id"], [(s, "next")]

    def _branch(self, field, value, b, sentence):
        then_first, then_tails = self.compile_seq(b.get("then") or [])
        else_first, else_tails = self.compile_seq(b.get("else") or [])
        if not then_first and not else_first:
            return "", []  # a check that changes nothing
        step = self.add(
            {
                "id": self.new_id("check"),
                "type": "branch",
                "field": field,
                "operator": "eq",
                "value": value,
                "on_true": then_first,
                "on_false": else_first,
            }
        )
        tails = then_tails + else_tails
        # An empty side goes straight on to whatever follows.
        if not then_first:
            tails.append((step, "on_true"))
        if not else_first:
            tails.append((step, "on_false"))
        self.reasons.append(sentence)
        return step["id"], tails

    def _b_if_tag(self, b):
        tag = slugify(str(b.get("tag") or ""))[:40]
        if not tag:
            self.dropped.append("a check without a tag")
            return "", []
        return self._branch(
            "tag",
            tag,
            b,
            f"Goes one way for customers tagged “{tag}”, another for everyone else.",
        )

    def _b_if_open(self, b):
        return self._branch(
            "within_business_hours", True, b, "Checks your opening hours."
        )

    def _b_tag(self, b):
        tag = slugify(str(b.get("tag") or ""))[:40]
        if not tag:
            self.dropped.append("a tag with no name")
            return "", []
        s = self.add(
            {"id": self.new_id("tag"), "type": "add_tag", "tag": tag, "next": ""}
        )
        self.reasons.append(f"Tags them “{tag}” so you can see who it reached.")
        return s["id"], [(s, "next")]

    def _b_untag(self, b):
        tag = slugify(str(b.get("tag") or ""))[:40]
        if not tag:
            return "", []
        s = self.add(
            {"id": self.new_id("untag"), "type": "remove_tag", "tag": tag, "next": ""}
        )
        return s["id"], [(s, "next")]

    def _b_track_interest(self, b):
        s = self.add(
            {
                "id": self.new_id("lead"),
                "type": "create_lead",
                "source": "automation",
                "next": "",
            }
        )
        return s["id"], [(s, "next")]

    def _b_mark_status(self, b):
        status = str(b.get("status") or "")
        if status not in ("contacted", "qualified", "lost"):
            self.dropped.append(f"marking them “{status}” (not a status)")
            return "", []
        s = self.add(
            {
                "id": self.new_id("status"),
                "type": "update_lead_status",
                "status": status,
                "next": "",
            }
        )
        return s["id"], [(s, "next")]

    def _b_hand_to_team(self, b):
        to = str(b.get("to") or "least_busy").strip()
        if "@" in to:
            from apps.accounts import notifications

            try:
                notifications.recipients(self.account, to)
            except notifications.NotifyError:
                self.needs.append(
                    f"{to} isn't on your team, so it goes to the least busy teammate."
                )
                to = ""
        else:
            to = ""
        assign = self.add(
            {
                "id": self.new_id("assign"),
                "type": "assign_conversation",
                "to": to,
                "next": "",
            }
        )
        message = self._text(b.get("message")) or "{contact} needs a reply."
        notify = self.add(
            {
                "id": self.new_id("notify"),
                "type": "notify_team",
                "to": "assignee",
                "text": message,
                "next": "",
            }
        )
        assign["next"] = notify["id"]
        self.reasons.append("Gives the conversation to a teammate and emails them.")
        return assign["id"], [(notify, "next")]

    def _b_notify_team(self, b):
        text = self._text(b.get("text")) or "{contact} needs attention."
        s = self.add(
            {
                "id": self.new_id("notify"),
                "type": "notify_team",
                "to": "owners",
                "text": text,
                "next": "",
            }
        )
        return s["id"], [(s, "next")]

    def _b_follow_up(self, b):
        try:
            hours = min(max(float(b.get("in_hours") or 24), 0.25), 24 * 60)
        except (TypeError, ValueError):
            hours = 24
        note = self._text(b.get("note"), 255) or "Follow up with {first_name}"
        s = self.add(
            {
                "id": self.new_id("followup"),
                "type": "create_followup",
                "due_in_hours": hours,
                "note": note,
                "next": "",
            }
        )
        self.reasons.append(
            "Puts a reminder on your team's follow-up list. Nothing extra is sent to the customer."
        )
        return s["id"], [(s, "next")]

    def _b_send_form(self, b):
        from apps.conversations.models import ConversationForm

        name = str(b.get("form") or "").strip()
        form = ConversationForm.objects.filter(
            account=self.account, name__iexact=name
        ).first()
        if form is None:
            self.needs.append(
                f"Choose which form to send (there's no form called “{name}”)."
            )
        s = self.add(
            {
                "id": self.new_id("form"),
                "type": "start_form",
                "form": form.public_id if form else "",
                "next": "",
                "on_timeout": "",
            }
        )
        return s["id"], [(s, "next")]

    def _b_classify(self, b):
        from apps.automation import ai_steps

        if "ai_classify" not in _engine_types():
            self.dropped.append("sorting messages with AI (not available)")
            return "", []
        labels = ai_steps.clean_labels(b.get("labels"))
        if len(labels) < 2:
            self.dropped.append("sorting with fewer than two labels")
            return "", []
        examples = b.get("examples") if isinstance(b.get("examples"), dict) else {}
        s = self.add(
            {
                "id": self.new_id("classify"),
                "type": "ai_classify",
                "labels": [
                    {
                        "label": label,
                        "examples": [str(e)[:200] for e in (examples.get(label) or [])][
                            :5
                        ],
                    }
                    for label in labels
                ],
                "routes": {},
                "on_unsure": "",
            }
        )
        self.reasons.append(
            "Sorts each message into "
            + ", ".join(labels)
            + "; anything unclear is left for your team."
        )
        return s["id"], [(s, "on_unsure")]


def _engine_types() -> set:
    from apps.automation.workflow_engine import _STEP_TYPES

    return set(_STEP_TYPES)


def _trigger(account, block, builder: _Builder) -> tuple[dict, list[dict], list[str]]:
    """``(trigger, leading steps, keywords)`` for a trigger block."""
    from apps.automation import keywords as kw

    kind = (block or {}).get("block") if isinstance(block, dict) else None
    if kind not in TRIGGER_BLOCKS:
        builder.needs.append(
            "Say when this should start (for example, when a customer messages you)."
        )
        kind = "when_message"
    words = []
    if kind == "when_message":
        words = [
            str(w).strip().lower()[: kw.MAX_KEYWORD_LENGTH]
            for w in block.get("keywords") or []
            if str(w).strip()
        ]
        words = list(dict.fromkeys(words))[: kw.MAX_KEYWORDS]
        trigger: dict[str, Any] = {"type": "conversation.message_received"}
        if words:
            trigger["match"] = {"mode": "contains", "any": words}
            builder.reasons.insert(
                0, f"Starts when a customer's message mentions {', '.join(words[:3])}."
            )
        return trigger, [], words
    if kind == "when_closed":
        trigger = {"type": "conversation.message_received", "cooldown_minutes": 480}
        check = {
            "id": builder.new_id("check"),
            "type": "branch",
            "field": "within_business_hours",
            "operator": "eq",
            "value": False,
            "on_true": "",
            "on_false": "",
        }
        builder.reasons.insert(
            0,
            "Only runs outside the opening hours you set, at most once every 8 hours per customer.",
        )
        return trigger, [check], []
    mapping = {
        "when_new_customer": "contact.created",
        "when_interested": "lead.created",
        "when_order_paid": "order.paid",
        "manual": "manual",
    }
    if kind in mapping:
        return {"type": mapping[kind]}, [], []
    if kind == "when_lead_status":
        status = block.get("status")
        return (
            {
                "type": {"qualified": "lead.qualified", "lost": "lead.lost"}.get(
                    status, "lead.status_changed"
                )
            },
            [],
            [],
        )
    # when_form_completed
    from apps.conversations.models import ConversationForm

    form = ConversationForm.objects.filter(
        account=account, name__iexact=str(block.get("form") or "")
    ).first()
    trigger = {"type": "conversation.form_completed"}
    if form:
        trigger["form"] = form.public_id
    return trigger, [], []


def compile_plan(account, plan: dict) -> dict[str, Any]:
    """The plan as a built automation, in the same shape ``intents.build_from_intent`` returns
    (plus ``needs``: things the owner should decide), ready for the review page."""
    from apps.automation import variables
    from apps.automation.workflow_engine import validate_definition

    plan = plan if isinstance(plan, dict) else {}
    b = _Builder(account)
    trigger, lead, words = _trigger(account, plan.get("trigger"), b)
    for step in lead:
        b.add(step)
    first, tails = b.compile_seq(plan.get("steps") or [])
    if lead:
        lead[0]["on_true"] = first
    if not first:
        b.needs.append("Add what it should do.")
    if b.steps:
        b.steps.append({"id": "stop", "type": "stop"})
        for t in tails:
            b._point(t, "stop")
        for s in b.steps:
            if s.get("type") == "branch":
                s["on_true"] = s.get("on_true") or "stop"
                s["on_false"] = s.get("on_false") or "stop"
    # The first step must come first: the engine starts at steps[0].
    entry = lead[0]["id"] if lead else first
    if entry:
        b.steps.sort(key=lambda s: 0 if s["id"] == entry else 1)
    definition = {"trigger": trigger, "steps": b.steps}

    warnings = [f"Left out {d}." for d in b.dropped]
    from apps.ai import api as ai_api

    for text in b.texts:
        unchecked = ai_api.unchecked_facts(
            account, variables.merge_first_name(text, "Sam")
        )
        if unchecked:
            warnings.append(
                "Check "
                + ", ".join(unchecked[:3])
                + ": it isn't in your notes, hours or answers."
            )
            break
    errors: list[str] = []
    more_warnings: list[str] = []
    for item in validate_definition(definition, account=account):
        (more_warnings if item.get("severity") == "warning" else errors).append(
            item["message"]
        )
    name = " ".join(str(plan.get("name") or "").split())[:100] or "My automation"
    return {
        "intent": "custom",
        "name": name,
        "slug": (slugify(name) or "my-automation")[:50],
        "definition": definition,
        "reply_text": b.texts[0] if b.texts else "",
        "preview": variables.merge_business_facts(b.texts[0], account)
        if b.texts
        else "",
        "keywords": words,
        "tag": "",
        "reasons": b.reasons[:5],
        "warnings": warnings + more_warnings,
        "errors": errors,
        "needs": b.needs,
        "editable": {"reply": False, "keywords": False},
    }
