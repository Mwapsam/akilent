"""AI drafts for setting Akilent up: it proposes, Akilent's own code builds.

Three kinds, each with a fixed output shape the model must fill and a deterministic check after:

* ``automation``: "describe what you want" or a pasted conversation -> ONE intent from
  ``apps.automation.intents.INTENTS`` plus a few words. ``build_from_intent`` then makes the real
  workflow, so the model never writes workflow structure. Unsure or unknown -> a clarifying message,
  never a guess.
* ``template``: "describe the message you need" -> the WhatsApp template form's fields, checked by
  ``template_builder.validate_fields`` and ``template_lint``. The owner submits it through the normal
  form, so Meta approval still applies.
* ``template_edit``: Friendlier / Shorter / Add our business name / Translate -> a new body with the
  exact same blanks, shown as a before/after diff.

All run in the background (``tasks.build_draft``) and never execute anything.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from apps.ai import prompts
from apps.ai.proposals import ProposalError, extract_json
from apps.ai.types import ChatMessage

MIN_CONFIDENCE = 0.6
MAX_PROMPT = 2000
_VAR = re.compile(r"\{\{\d+\}\}")

INTENT_DESCRIPTIONS = {
    "answer_pricing": "reply when customers ask about prices or cost",
    "answer_location": "reply when customers ask where the business is",
    "answer_hours": "reply when customers ask about opening hours",
    "answer_delivery": "reply when customers ask about delivery",
    "answer_payment": "reply when customers ask how to pay",
    "reply_when_closed": "reply to messages that arrive outside opening hours",
    "welcome_new": "greet first-time customers",
    "hand_interested_to_team": "give interested customers to a teammate and tell the team",
    "offer_menu": "greet people with a menu of buttons (up to 3 options)",
    "follow_up_quiet": "check in with a customer who went quiet, using an approved template",
    "answer_question": "reply to any other specific question (needs keywords and a reply)",
}

EDITS = {
    "friendlier": "Make it warmer and friendlier, same meaning, similar length.",
    "shorter": "Make it shorter and clearer, keeping every fact.",
    "business_name": "Mention the business name naturally, once.",
}

CATEGORY_REASONS = {
    "utility": "Utility, because it's about something the customer is already doing with you (an order, a booking, a payment).",
    "marketing": "Marketing, because it promotes something. It will only go to customers who agreed to marketing messages.",
    "authentication": "Authentication, because it sends a one-time code.",
}


class DraftError(ProposalError):
    """The draft can't be used; the message is for the owner."""


# ---- prompts ---------------------------------------------------------------------------------
def _business_block(account, business_notes: str) -> str:
    from apps.accounts import api as accounts_api

    lines = [f"Business name: {account.company_name or 'the business'}"]
    for key, value in accounts_api.business_facts(account).items():
        lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    hours = accounts_api.business_hours_text(account)
    if hours:
        lines.append(f"Opening hours: {hours}")
    if business_notes.strip():
        lines.append("Notes: " + business_notes.strip()[:1500])
    return "\n".join(lines)


def automation_prompt(account, business_notes: str) -> str:
    intents = "\n".join(f'- "{k}": {v}' for k, v in INTENT_DESCRIPTIONS.items())
    return f"""You help a small business owner set up WhatsApp automations. You only choose WHAT they \
want from a fixed list; Akilent builds the automation itself.

Intents:
{intents}

Answer with ONE JSON object and nothing else:
{{"intent": "<one key above, or \\"none\\">", "confidence": 0.0-1.0,
 "entities": {{"keywords": ["<3-8 lowercase words or short phrases customers would type>"],
              "reply_text": "<the reply to send, only if needed>", "topic_label": "<2-3 words>",
              "question": "<menu question>", "options": [{{"title": "<max 20 chars>", "reply": "..."}}]}}}}

Rules:
- If the request doesn't fit an intent, or you're unsure, answer {{"intent": "none", "confidence": 0}}.
- If the owner pasted a conversation, copy the business's own reply word for word as reply_text.
- Otherwise write reply_text only from the facts below; never invent prices, times or promises.
- Include only the entities that intent needs.

About the business:
{_business_block(account, business_notes)}"""


def template_prompt(account, business_notes: str) -> str:
    return f"""You write WhatsApp message templates that Meta will approve, for a small business.

Answer with ONE JSON object and nothing else:
{{"category": "utility" | "marketing" | "authentication", "name": "<lowercase_with_underscores>",
 "language": "<Meta code, e.g. en>", "header": "<optional, max 60 chars>",
 "body": "<the message, blanks as {{{{1}}}}, {{{{2}}}}...>", "footer": "<optional, max 60 chars>",
 "variables": [{{"label": "<what the blank is, e.g. Customer's first name>", "example": "<realistic example>"}}]}}

Meta's rules you must follow:
- Utility = about something the customer is already doing (order, booking, payment, delivery).
  No promotional words (discount, offer, sale, free...) in utility.
- Marketing = promotions and news. Authentication = one-time codes only; WhatsApp writes that
  wording itself, so for authentication leave header, body and footer empty and variables [].
- Blanks are numbered in order from {{{{1}}}}; one variables entry per blank, in order.
- Never start or end the body with a blank, and never put two blanks side by side.
- Use a blank for anything that changes per customer (their name, an order number, an amount).
- Short, friendly, plain words. No facts that aren't below.

About the business:
{_business_block(account, business_notes)}"""


# ---- checks ----------------------------------------------------------------------------------
def check_automation(account, data: dict) -> tuple[dict, list[str]]:
    """``({"intent", "entities", "confidence"}, warnings)`` or ``DraftError`` with a clarifying message."""
    from apps.automation.intents import INTENTS, IntentError, build_from_intent

    intent = str(data.get("intent") or "").strip()
    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if intent not in INTENTS or confidence < MIN_CONFIDENCE:
        raise DraftError("I couldn't tell what you want to automate. Try something like "
                         "“when someone asks about delivery, tell them we deliver in Lusaka”.")
    entities = data.get("entities") if isinstance(data.get("entities"), dict) else {}
    try:
        built = build_from_intent(account, intent, entities)
    except IntentError as exc:
        raise DraftError(str(exc)) from exc
    return {"intent": intent, "entities": entities, "confidence": confidence}, built["warnings"]


def _snake(name: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (name or "").lower())).strip("_")[:60]


def check_template(data: dict) -> tuple[dict, list[str]]:
    from apps.whatsapp.api import lint_template, validate_template_fields

    fields = {
        "category": str(data.get("category") or "utility").lower(),
        "name": _snake(str(data.get("name") or "")) or "new_message",
        "language": str(data.get("language") or "en"),
        "header": str(data.get("header") or "")[:60], "body": str(data.get("body") or "").strip(),
        "footer": str(data.get("footer") or "")[:60],
    }
    if fields["category"] == "authentication":
        # WhatsApp writes a one-time code's wording itself, so any text the model wrote is dropped;
        # the form's One-time code options (security line, expiry, button) are all that's needed.
        fields.update(header="", body="", footer="", variables=[])
        return fields, []
    variables = [v for v in data.get("variables") or [] if isinstance(v, dict)]
    labels = [str(v.get("label") or "").strip() for v in variables]
    examples = [str(v.get("example") or "").strip() for v in variables]
    problem = validate_template_fields(**fields, variable_labels=labels, variable_examples=examples)
    if problem:
        raise DraftError(f"The draft wasn't usable ({problem}). Try describing it again.")
    fields["variables"] = [{"label": l, "example": e} for l, e in zip(labels, examples)]
    warnings = [w["text"] for w in lint_template(category=fields["category"], language=fields["language"],
                                                 body=fields["body"], header=fields["header"], footer=fields["footer"])]
    return fields, warnings


def template_reasons(fields: dict, warnings: list[str]) -> list[str]:
    """"Why I built it this way", from fixed rules about the draft, never the model's own words."""
    from apps.automation.variables import looks_like_name

    reasons = [CATEGORY_REASONS.get(fields.get("category"), "")]
    if fields.get("category") == "authentication":
        return [r for r in reasons if r] + [
            "WhatsApp writes the wording for codes, so choose the security line, expiry and button below."]
    if fields.get("category") == "utility" and not any("marketing" in w.lower() for w in warnings):
        reasons.append("No promotional wording, so Meta should accept it as Utility.")
    for n, var in enumerate(fields.get("variables") or [], start=1):
        if looks_like_name(var["label"]):
            reasons.append(f"{{{{{n}}}}} is the customer's first name, filled in for each person when it's sent.")
            break
    if not any("blank" in w.lower() for w in warnings):
        reasons.append("Blanks sit inside sentences, as Meta requires.")
    return [r for r in reasons if r][:4]


def word_diff(before: str, after: str) -> list[list[str]]:
    """``[[op, text]]`` with op "same" | "add" | "del", word by word, for the before/after view."""
    a, b = before.split(" "), after.split(" ")
    out = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b).get_opcodes():
        if op == "equal":
            out.append(["same", " ".join(a[i1:i2])])
        else:
            if i2 > i1:
                out.append(["del", " ".join(a[i1:i2])])
            if j2 > j1:
                out.append(["add", " ".join(b[j1:j2])])
    return out


def check_edit(before: str, data: dict) -> dict:
    after = str(data.get("body") or "").strip()
    if not after:
        raise DraftError("The edit came back empty. Try again.")
    if sorted(_VAR.findall(after)) != sorted(_VAR.findall(before)):
        raise DraftError("The edit changed the blanks ({{1}}…), so it wasn't used. Try again.")
    return {"before": before, "after": after, "diff": word_diff(before, after)}


# ---- running one draft ------------------------------------------------------------------------
def run(draft, provider) -> None:
    """Fill ``draft`` (an ``AIDraft``) from the model. Raises ``AIProviderError`` / ``DraftError``."""
    from apps.ai.models import AISettings

    account = draft.account
    notes = AISettings.objects.filter(account=account).values_list("business_notes", flat=True).first() or ""
    if draft.kind == "automation":
        system = automation_prompt(account, notes)
        user = draft.prompt
        if draft.context.get("conversation"):
            user += "\n\nThe conversation they pasted:\n" + draft.context["conversation"]
    elif draft.kind == "template":
        system, user = template_prompt(account, notes), draft.prompt
    else:
        instruction = draft.context.get("instruction", "")
        how = EDITS.get(instruction) or (
            f"Translate it into the language with code {instruction.split(':', 1)[1]}."
            if instruction.startswith("translate:") else "")
        if not how:
            raise DraftError("Unknown edit.")
        system = ("You edit WhatsApp message templates. Keep every blank like {{1}} exactly as it is, "
                  'in the same order. Answer with ONE JSON object: {"body": "<the new message>"}.')
        user = f"{how}\nBusiness name: {account.company_name}\n\nMessage:\n{draft.context.get('body', '')}"

    result = provider.chat([ChatMessage("user", user)], system=system, max_tokens=1500, temperature=0.3)
    data = extract_json(result.text)
    if draft.kind == "automation":
        draft.result, draft.warnings = check_automation(account, data)
    elif draft.kind == "template":
        fields, warnings = check_template(data)
        draft.result, draft.warnings = {**fields, "reasons": template_reasons(fields, warnings)}, warnings
    else:
        draft.result = check_edit(draft.context.get("body", ""), data)
        from apps.whatsapp.api import lint_template

        instruction = draft.context.get("instruction", "")
        language = instruction.split(":", 1)[1] if instruction.startswith("translate:") else draft.context.get("language", "en")
        draft.warnings = [w["text"] for w in lint_template(
            category=draft.context.get("category", "utility"), language=language, body=draft.result["after"])]
        if instruction.startswith("translate:") and language != draft.context.get("language"):
            draft.warnings.append("If you use this, change the template's language to match before submitting.")
    draft.model = (result.model or "")[:80]


def clean_prompt(text: str) -> str:
    return prompts.mask((text or "").strip())[:MAX_PROMPT]


def as_json(draft) -> dict:
    return {"id": draft.pk, "kind": draft.kind, "status": draft.status, "result": draft.result,
            "warnings": draft.warnings, "error": draft.error}
