"""What an owner can ask Akilent to automate, and how each request becomes a real workflow.

AI (or a recommendation, or a reply the team keeps sending) only ever names an **intent** from
this fixed catalogue plus a few entities (keywords, reply text, a topic). ``build_from_intent``
then decides everything else: which starter and builder, which tag, which trigger, and the plain
"Why I built it this way" reasons. So no model ever writes workflow structure, and every draft is
a shape the engine already runs and ``validate_definition`` already checks.

Works with AI off: recommendations and the "you've answered this N times" button call it directly.
"""
from __future__ import annotations

from django.utils.text import slugify

from apps.automation import keywords as kw
from apps.automation.engagement_starters import (
    STARTERS_BY_KEY,
    build_definition,
    build_menu_definition,
    build_reply_definition,
    build_team_definition,
)

MAX_REPLY = 1000

# key -> how to build it. "starter" is the engagement starter it reuses (its key is the workflow
# slug, so the goal gallery shows it as installed). "reply" is the text used when nobody supplied
# one: placeholders like {location} are filled from the owner's profile at send time.
INTENTS = {
    "answer_pricing": {
        "label": "Answer pricing questions", "starter": "answer-pricing-questions",
        "reply": "Thanks for asking! Tell us what you need and we'll send you an exact price.",
    },
    "answer_location": {
        "label": "Answer location questions", "starter": "answer-where-are-you",
        "reply": "We're at {location}.", "needs": "location",
    },
    "answer_hours": {
        "label": "Answer questions about your opening hours", "slug": "answer-opening-hours",
        "keywords": ["open", "opening hours", "closing time", "what time", "are you open", "hours"],
        "tag": "asked-hours", "reply": "We're open {opening_hours}.", "needs": "opening_hours",
    },
    "answer_delivery": {
        "label": "Answer delivery questions", "slug": "answer-delivery-questions",
        "keywords": ["deliver", "delivery", "deliveries", "shipping", "transport"],
        "tag": "asked-delivery", "reply": "{delivery}", "needs": "delivery",
    },
    "answer_payment": {
        "label": "Answer payment questions", "slug": "answer-payment-questions",
        "keywords": ["pay", "payment", "momo", "mobile money", "bank transfer", "cash", "card"],
        "tag": "asked-payment", "reply": "You can pay with {payment_methods}.", "needs": "payment_methods",
    },
    "reply_when_closed": {
        "label": "Reply when you're closed", "starter": "reply-when-closed", "needs": "opening_hours",
        "reply": "Thanks for your message! We're closed right now. Our hours are {opening_hours}, "
                 "and we'll reply as soon as we open.",
    },
    "welcome_new": {
        "label": "Welcome new customers", "slug": "welcome-new-customers", "tag": "welcomed",
        "reply": "Hi {first_name}, thanks for getting in touch! How can we help?",
    },
    "hand_interested_to_team": {
        "label": "Hand new interested customers to your team", "starter": "route-new-leads",
        "reply": "{contact} is interested. Please reply to them soon.",
    },
    "offer_menu": {"label": "Offer a menu when someone says hello", "starter": "offer-a-menu"},
    "follow_up_quiet": {"label": "Check in when a customer goes quiet", "starter": "quiet-customer-check-in"},
    "answer_question": {"label": "Answer a common question"},
}
# Entities each intent accepts; anything else the model sends is ignored.
_ACCEPTS = {
    "answer_question": {"keywords", "reply_text", "topic_label", "tag"},
    "offer_menu": {"question", "options"},
    "follow_up_quiet": {"template"},
}
_DEFAULT_ACCEPTS = {"keywords", "reply_text", "tag"}


class IntentError(ValueError):
    """The request can't become an automation; the message is for the owner."""


def _clean_keywords(values) -> list[str]:
    out = []
    for value in values or []:
        word = str(value).strip().lower()[: kw.MAX_KEYWORD_LENGTH]
        if word and word not in out:
            out.append(word)
    return out[: kw.MAX_KEYWORDS]


def _reply_starter(intent: dict, starter: dict | None, keywords: list[str], tag: str | None) -> dict:
    """A starter-shaped dict for ``build_reply_definition``: the real starter, or one made here."""
    base = dict(starter or {"trigger": "conversation.message_received", "mode": "contains"})
    base.update({"name": intent["label"], "keywords": keywords or base.get("keywords", []), "tag": tag})
    return base


def _welcome_definition(text: str) -> dict:
    """Welcome someone the first time they write, once: the "welcomed" tag remembers who was welcomed.

    A plain reply (no template needed), because the customer has just written.
    """
    return {
        "trigger": {"type": "conversation.message_received"},
        "steps": [
            {"id": "new", "type": "branch", "field": "tag", "operator": "ne", "value": "welcomed",
             "on_true": "reply", "on_false": "stop"},
            {"id": "reply", "type": "reply_text", "text": text, "next": "tag"},
            {"id": "tag", "type": "add_tag", "tag": "welcomed", "next": "stop"},
            {"id": "stop", "type": "stop"},
        ],
    }


def _menu_options(account, entities: dict) -> list[dict]:
    options = []
    for option in entities.get("options") or []:
        if isinstance(option, dict) and str(option.get("title") or "").strip() and str(option.get("reply") or "").strip():
            options.append({"title": str(option["title"]).strip()[:20], "reply": str(option["reply"]).strip()[:MAX_REPLY]})
    if options:
        return options[:3]
    from apps.accounts import api as accounts_api

    defaults = []
    if accounts_api.business_fact(account, "location"):
        defaults.append({"title": "Where are you?", "reply": "We're at {location}."})
    if accounts_api.business_fact(account, "opening_hours"):
        defaults.append({"title": "Opening hours", "reply": "We're open {opening_hours}."})
    if accounts_api.business_fact(account, "payment_methods"):
        defaults.append({"title": "How to pay", "reply": "You can pay with {payment_methods}."})
    return defaults[:3] or [{"title": "Talk to us", "reply": "Thanks! Someone from our team will reply shortly."}]


def _published_clashes(account, keywords: list[str], slug: str) -> list[str]:
    """Live automations that would also answer a message containing one of ``keywords``."""
    from apps.automation.models import Workflow

    clashes = []
    for wf in Workflow.objects.filter(account=account, status=Workflow.Status.PUBLISHED).exclude(slug=slug):
        match = ((wf.definition or {}).get("trigger") or {}).get("match")
        if match and any(kw.matches(match, word) for word in keywords):
            clashes.append(wf.name)
    return clashes


def build_from_intent(account, intent_key: str, entities: dict | None = None, *, evidence: dict | None = None) -> dict:
    """The real workflow for one intent, with its explanation. Never saves anything.

    Returns ``{"intent", "name", "slug", "definition", "reply_text", "preview", "keywords", "tag",
    "reasons", "warnings", "errors", "editable"}``. ``errors`` block turning it on; ``warnings``
    don't. ``evidence`` (e.g. ``{"team_reply_count": 9}``) only adds reasons.

    Raises ``IntentError`` for an unknown intent or one that can't be built for this business.
    """
    from apps.accounts import api as accounts_api
    from apps.automation import variables
    from apps.automation.workflow_engine import validate_definition

    intent = INTENTS.get(intent_key)
    if intent is None:
        raise IntentError("That isn't something Akilent can automate yet.")
    accepts = _ACCEPTS.get(intent_key, _DEFAULT_ACCEPTS)
    entities = {k: v for k, v in (entities or {}).items() if k in accepts and v not in (None, "", [])}
    evidence = evidence or {}
    starter = STARTERS_BY_KEY.get(intent.get("starter", ""))
    reasons, warnings = [], []

    # The words to reply with: what the team already sends > what the owner or AI wrote > the
    # intent's default, which reads the owner's own answers.
    reply = str(entities.get("reply_text") or "").strip()[:MAX_REPLY]
    if evidence.get("team_reply_count"):
        reasons.append(f"Reuses the reply your team has sent {evidence['team_reply_count']} times.")
    elif not reply and intent.get("reply"):
        reply = intent["reply"]
    needs = intent.get("needs")
    if needs and "{" + needs + "}" in reply and not accounts_api.business_fact(account, needs):
        raise IntentError({
            "location": "Tell Akilent where you are first (Build, step 1).",
            "opening_hours": "Set your opening hours first (Build, step 1).",
            "delivery": "Tell Akilent whether you deliver first (Build, step 1).",
            "payment_methods": "Tell Akilent how customers pay first (Build, step 1).",
        }[needs])
    if any("{" + key + "}" in reply for key in ("location", "opening_hours", "delivery", "payment_methods", "website")):
        reasons.append("Uses your answers from “Tell us about your business”, so it stays up to date when you change them.")

    keywords = _clean_keywords(entities.get("keywords")) or list(intent.get("keywords") or (starter or {}).get("keywords") or [])
    tag = entities.get("tag") or intent.get("tag") or (starter or {}).get("tag")
    if intent_key == "answer_question":
        topic = str(entities.get("topic_label") or (keywords[0] if keywords else "")).strip()[:40]
        if not keywords:
            raise IntentError("Say which words the customer's question contains, e.g. “installation”.")
        if not reply:
            raise IntentError("Write the reply to send.")
        tag = tag or (f"asked-{slugify(topic)}"[:40] if topic else None)
        name = f"Answer questions about {topic}" if topic else intent["label"]
        slug = f"answer-{slugify(topic)}"[:50] if slugify(topic) else "answer-a-common-question"
    else:
        name, slug = intent["label"], intent.get("starter") or intent.get("slug")
    tag = slugify(str(tag))[:40] if tag else None

    if intent_key == "welcome_new":
        definition = _welcome_definition(reply)
        reasons.append("Answers a customer's first message once; the tag “welcomed” makes sure it's never repeated.")
    elif intent_key == "reply_when_closed":
        definition = build_reply_definition(starter, text=reply)
        reasons.append("Only replies outside the opening hours you set, at most once every 8 hours per customer.")
    elif intent_key == "hand_interested_to_team":
        definition = build_team_definition(starter, text=reply)
        reasons.append("Gives each new interested customer to your least busy teammate and emails them.")
    elif intent_key == "offer_menu":
        options = _menu_options(account, entities)
        question = str(entities.get("question") or "Hi {first_name}! What can we help you with?")[:1024]
        definition = build_menu_definition(starter, question=question, options=options)
        reply = question
        reasons.append("Offers " + ", ".join(o["title"] for o in options) + " as buttons, built from your answers.")
    elif intent_key == "follow_up_quiet":
        from apps.whatsapp import api as whatsapp_api

        template = whatsapp_api.approved_template_by_name(account, str(entities.get("template") or ""))
        if template is None:
            template = whatsapp_api.first_approved_template(account)
        if template is None:
            raise IntentError("This needs an approved WhatsApp template. Create one first (Build, “Create a template”).")
        mapping, fallbacks = {}, {}
        for var in template.variables or []:
            choice = variables.default_choice(var)
            source, fallback = variables.entry_from_choice(choice if choice != "literal" else "first_name")
            mapping[var], fallbacks[var] = source, fallback
        definition = build_definition(starter, template_name=template.whatsapp_template_name,
                                      variable_mapping=mapping, variable_fallbacks=fallbacks)
        reply = template.content or ""
        reasons.append(f"Sends your approved template “{template.name}” three days after a customer goes quiet, "
                       "and only if they haven't written back.")
    else:
        problems = kw.validate({"mode": "contains", "any": keywords})
        if problems:
            raise IntentError(problems[0])
        definition = build_reply_definition(_reply_starter(intent, starter, keywords, tag), text=reply)
        shown = ", ".join(keywords[:3]) + (" …" if len(keywords) > 3 else "")
        reasons.insert(0, f"Starts when a customer's message mentions {shown}.")
        if tag:
            reasons.append(f"Tags them “{tag}” so you can see who asked and follow up.")
        clashes = _published_clashes(account, keywords, slug)
        if clashes:
            warnings.append(f"“{clashes[0]}” already answers some of these messages, so both would reply.")

    preview = variables.merge_business_facts(reply, account)
    if reply and intent_key not in ("follow_up_quiet", "hand_interested_to_team"):
        from apps.ai import api as ai_api

        unchecked = ai_api.unchecked_facts(account, variables.merge_first_name(preview, "Sam"))
        if unchecked:
            warnings.append("Check " + ", ".join(unchecked[:3]) + ": it isn't in your notes, hours or answers, "
                            "so make sure it's right before turning this on.")
    errors = []
    for item in validate_definition(definition, account=account):
        (warnings if item.get("severity") == "warning" else errors).append(item["message"])
    from apps.automation.models import Workflow

    if Workflow.objects.filter(account=account, slug=slug, status=Workflow.Status.PUBLISHED).exists():
        warnings.append("You already have this automation on. Turning this on replaces it.")
    return {
        "intent": intent_key, "name": name, "slug": slug, "definition": definition,
        "reply_text": reply, "preview": preview, "keywords": keywords, "tag": tag or "",
        "reasons": reasons[:4], "warnings": warnings, "errors": errors,
        "editable": {
            "reply": intent_key not in ("offer_menu", "follow_up_quiet"),
            "keywords": "trigger" in definition and bool((definition["trigger"] or {}).get("match"))
                        and intent_key != "offer_menu",
        },
    }
