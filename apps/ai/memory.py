"""Conversation memory: the earlier part of a long thread, folded into a short summary and facts.

The drafting prompt carries the last ``prompts.RECENT_MESSAGES`` messages word for word. Once enough
older messages pile up beyond that window, a background task folds them into
``AIConversationMemory`` (the previous summary plus the new older messages in, a new summary out).
Incremental and cheap: each message is summarised once. Same privacy rules as drafting: phone
numbers and emails masked, no system lines, no notes, nothing from other conversations.
"""
from __future__ import annotations

import json

from apps.ai import prompts
from apps.ai.proposals import ProposalError, extract_json
from apps.ai.types import ChatMessage

# Fold older messages in batches, not one at a time.
REFRESH_AFTER = 6
BATCH = 40
MAX_SUMMARY = 1200
MAX_FACTS = 10

SYSTEM = """You keep short notes on a customer conversation for a small business's team.

You get the notes so far (maybe empty) and the next messages, oldest first. "Customer:" lines are \
the customer, "Business:" lines are the business. Update the notes.

Answer with ONE JSON object and nothing else:
{"summary": "<at most 5 short sentences: what the customer wants, what was offered or agreed, \
what is still open>", "facts": {"<short key>": "<short value>"}}

facts: at most 10 things the customer said about themselves or their request (what they want, \
quantity, town, budget, preferred day). Keys like "wants", "town", "budget". Never include phone \
numbers, emails or payment details. Keep facts from the old notes unless the customer changed them."""


def memory_for(conversation):
    from apps.ai.models import AIConversationMemory

    return AIConversationMemory.objects.filter(conversation=conversation).first()


def pending_messages(conversation, memory=None) -> list[dict]:
    from apps.conversations.api import earlier_messages

    after = memory.covered_until_id if memory else 0
    return earlier_messages(conversation, keep_recent=prompts.RECENT_MESSAGES, after_id=after, limit=BATCH)


def needs_refresh(conversation) -> bool:
    return len(pending_messages(conversation, memory_for(conversation))) >= REFRESH_AFTER


def _clean(data: dict, old_facts: dict) -> tuple[str, dict]:
    summary = prompts.mask(str(data.get("summary") or "")).strip()[:MAX_SUMMARY]
    if not summary:
        raise ProposalError("The AI returned an empty summary.")
    facts = {}
    raw = data.get("facts") if isinstance(data.get("facts"), dict) else old_facts
    for key, value in list(raw.items())[:MAX_FACTS]:
        key, value = str(key).strip()[:40], prompts.mask(str(value)).strip()[:120]
        if key and value:
            facts[key] = value
    return summary, facts


def refresh(conversation, provider) -> bool:
    """Fold the older messages not yet covered into this conversation's memory. True if updated.

    Raises ``AIProviderError`` / ``ProposalError`` like drafting does; nothing is changed then.
    """
    from apps.ai.models import AIConversationMemory

    memory = memory_for(conversation)
    batch = pending_messages(conversation, memory)
    if not batch:
        return False
    old = {"summary": memory.summary if memory else "", "facts": memory.facts if memory else {}}
    lines = [f"{'Customer' if m['direction'] == 'inbound' else 'Business'}: {prompts.mask(m['body']).strip()}"
             for m in batch]
    prompt = "Notes so far:\n" + json.dumps(old, ensure_ascii=False) + "\n\nNext messages:\n" + "\n".join(lines)
    result = provider.chat([ChatMessage("user", prompt)], system=SYSTEM, max_tokens=1200, temperature=0.1)
    summary, facts = _clean(extract_json(result.text), old["facts"] or {})
    AIConversationMemory.objects.update_or_create(
        conversation=conversation,
        defaults={"account": conversation.account, "summary": summary, "facts": facts,
                  "covered_until_id": batch[-1]["id"], "model": (result.model or "")[:80]},
    )
    return True
