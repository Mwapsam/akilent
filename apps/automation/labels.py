"""Plain-language labels for workflow triggers/steps, shown wherever a business
owner sees a workflow (list page, editor). Never the only place the raw type
lives — `Workflow.definition` keeps the real trigger/step type strings the
engine (workflow_engine.py) understands; this only translates them for display.
See docs/plans amendment (R1): "Don't simplify the engine, simplify the mental
model."
"""

TRIGGER_LABELS = {
    "manual": "Run manually",
    "business_event": "When a custom event happens",
    "contact.created": "When a new customer is added",
    "contact.updated": "When a customer's details change",
    "email.opened": "When a customer opens an email",
    "email.clicked": "When a customer clicks a link in an email",
    "whatsapp.received": "When a customer messages you on WhatsApp",
    "conversation.message_received": "When a customer messages you on WhatsApp, Instagram or Facebook",
    "lead.created": "When someone becomes an interested customer",
    "lead.status_changed": "When an interested customer's status changes",
    "lead.qualified": "When an interested customer is marked qualified",
    "lead.lost": "When an interested customer is marked lost",
    "deal.created": "When a deal is created",
    "deal.stage_changed": "When a deal moves to another stage",
    "order.created": "When an order is placed",
    "order.paid": "When an order is paid",
    "conversation.form_completed": "When a customer finishes a form",
    "website_chat.opened": "When a visitor opens your website chat",
    "website_chat.message_received": "When a visitor messages your website chat",
}

STEP_LABELS = {
    "reply_text": "Reply in the conversation",
    "send_buttons": "Ask with buttons",
    "send_list": "Ask with a list",
    "wait_for_reply": "Wait for their answer",
    "add_tag": "Add a tag",
    "remove_tag": "Remove a tag",
    "send_whatsapp": "Send a WhatsApp message",
    "send_email": "Send an email",
    "create_lead": "Track as interested",
    "update_lead_status": "Update how interested they are",
    "assign_conversation": "Assign to a teammate",
    "notify_team": "Tell your team",
    "webhook": "Advanced integration",
    "wait": "Wait",
    "branch": "If / otherwise",
    "set_attribute": "Update customer information",
    "stop": "Stop",
    "exit": "Stop",
    "action": "Do this",
    "ask_question": "Ask a question and save the answer",
    "lookup_product": "Look up a product",
    "publish_post": "Publish a post",
    "start_form": "Send a form",
    "create_followup": "Set a follow-up for your team",
    "ai_classify": "Sort the message (AI)",
    "ai_extract": "Pick out details (AI)",
    "ai_answer": "Answer from your facts (AI)",
}

# Step types a non-technical owner doesn't need to see unless they opt into
# "Advanced" — they're either developer-facing (webhook) or expose raw
# field/value inputs with no business-language equivalent yet (set_attribute).
ADVANCED_STEP_TYPES = {"webhook", "set_attribute", "action"}


def trigger_label(trigger_type: str, event_name: str = "") -> str:
    label = TRIGGER_LABELS.get(trigger_type or "", trigger_type or "—")
    if trigger_type == "business_event" and event_name:
        return f"{label}: {event_name}"
    return label


def step_label(step_type: str) -> str:
    return STEP_LABELS.get(step_type or "", step_type or "Step")
