"""AI's own records: a business's opt-in, and the proposals AI makes.

Nothing here is read by the engine or the inbox to decide anything. Every row can be deleted and
Akilent keeps working exactly as before, which is the point: no domain model gains an AI column.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models


class AISettings(models.Model):
    """One business's AI switch and the facts AI may use. Off until the owner opts in."""

    account = models.OneToOneField(
        "accounts.Account", on_delete=models.CASCADE, related_name="ai_settings"
    )
    enabled = models.BooleanField(default=False)
    # Prices, address, hours, policies: the only facts AI may state about the business.
    business_notes = models.TextField(blank=True, default="")
    consented_at = models.DateTimeField(null=True, blank=True)
    consented_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class ReplyMode(models.TextChoices):
        SUGGEST = "suggest", "Suggest replies for my team to send"
        AUTO = "auto", "Reply automatically to the questions I choose"

    # Whether AI may send some replies on its own (see apps.ai.autonomy). Suggest-only by default.
    reply_mode = models.CharField(
        max_length=10, choices=ReplyMode.choices, default=ReplyMode.SUGGEST
    )
    # Which kinds of question it may answer alone: keys of apps.ai.autonomy.TOPICS.
    auto_topics = models.JSONField(default=list, blank=True)
    auto_min_confidence = models.FloatField(default=0.85)
    auto_only_when_closed = models.BooleanField(default=False)
    # In automatic mode, when AI can't answer a message itself: tell the customer once that a
    # person will reply, then leave it to the team (apps.ai.autonomy.should_hold).
    holding_reply_enabled = models.BooleanField(default=True)
    holding_reply_text = models.CharField(max_length=300, blank=True, default="")
    auto_consented_at = models.DateTimeField(null=True, blank=True)
    auto_consented_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)
    # G — BM25 knowledge retrieval (per-account feature flag).
    knowledge_version = models.PositiveIntegerField(default=1)
    bm25_enabled = models.BooleanField(default=False)

    def __str__(self):
        return f"AI settings for account {self.account_id} ({'on' if self.enabled else 'off'}, {self.reply_mode})"


class KnowledgeBaseEntry(models.Model):
    """A fact an owner writes once so AI can answer from it — an FAQ answer, a policy, a short
    document — the same trust boundary as ``AISettings.business_notes`` (see ``apps.ai.facts``),
    just organized as named entries instead of one free-text block. Manually authored only: no
    document upload/ingestion here, deliberately a smaller first version.
    """

    class SourceType(models.TextChoices):
        FAQ = "faq", "FAQ"
        POLICY = "policy", "Policy"
        OTHER = "other", "Other"

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="knowledge_base_entries",
    )
    source_type = models.CharField(
        max_length=20, choices=SourceType.choices, default=SourceType.FAQ
    )

    class Origin(models.TextChoices):
        MANUAL = "manual", "Written by hand"
        INBOX = "inbox", "From a conversation"
        IMPORT = "import", "Imported"
        WEBSITE = "website", "From your website"
        AI_DRAFT = "ai_draft", "Drafted by AI"

    title = models.CharField(max_length=200)
    content = models.TextField()
    # Imported, scraped and AI-drafted entries start off (is_active=False) until a person
    # approves them: "Needs review" on the knowledge page.
    is_active = models.BooleanField(default=True)
    origin = models.CharField(
        max_length=20, choices=Origin.choices, default=Origin.MANUAL
    )
    source_url = models.URLField(max_length=500, blank=True, default="")
    # When a person approved an imported, scraped or AI-drafted entry. Until then it sits under
    # "Needs review" and AI never sees it.
    reviewed_at = models.DateTimeField(null=True, blank=True)

    REVIEW_ORIGINS = ("import", "website", "ai_draft")

    @property
    def needs_review(self) -> bool:
        return self.origin in self.REVIEW_ORIGINS and self.reviewed_at is None

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title


class AIProposal(models.Model):
    """Something AI proposes doing in a conversation. A person decides; AI never acts.

    ``version``/``action``/``confidence``/``reason``/``payload`` are the proposal contract (see
    ``apps.ai.proposals``). The outcome fields exist from day one so acceptance and edit rates can
    be measured without a later migration.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # queued, the model hasn't answered yet
        READY = "ready", "Ready"  # a valid proposal is waiting for a person
        ERROR = "error", "Error"  # the model failed or proposed something invalid
        USED = "used", "Used"  # a person sent it (possibly edited)
        DISMISSED = "dismissed", "Dismissed"
        EXPIRED = "expired", "Expired"  # overtaken by a newer message or a human reply

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="ai_proposals"
    )
    conversation = models.ForeignKey(
        "conversations.Conversation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    trigger_message = models.ForeignKey(
        "conversations.Message",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    version = models.PositiveSmallIntegerField(default=1)
    action = models.CharField(max_length=32, blank=True, default="")
    confidence = models.FloatField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    provider = models.CharField(max_length=40, blank=True, default="")
    model = models.CharField(max_length=80, blank=True, default="")
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    edited_before_send = models.BooleanField(null=True, blank=True)
    # Side suggestions shown as one-click chips (tag, track interest, follow-up). Each item is the
    # contract's extra plus "applied_at" once a person clicked it. See apps.ai.proposals.
    extras = models.JSONField(default=list, blank=True)
    # The look-ups the model asked for while drafting, in order, e.g. ["check_opening_hours"].
    tools_used = models.JSONField(default=list, blank=True)
    # What the customer asked about (apps.ai.proposals.INTENTS) and which model tier answered.
    intent = models.CharField(max_length=20, blank=True, default="")
    route = models.CharField(max_length=10, blank=True, default="")
    # The audit trail for replying on its own: every check and whether it passed, and when it sent.
    # Empty when the business is suggest-only.
    auto_decision = models.JSONField(default=dict, blank=True)
    auto_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["account", "status", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "trigger_message"],
                condition=models.Q(trigger_message__isnull=False),
                name="one_ai_proposal_per_message",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"AI proposal {self.pk} ({self.action or 'pending'}, {self.status})"


class AIDraft(models.Model):
    """Something AI drafted for setting Akilent up: an automation request, a template, a template edit.

    Never the real thing. ``result`` is only an intent plus words (automations), or form fields
    (templates); the owner reviews it and Akilent's own builders make the real workflow or template
    (see ``apps.ai.drafting``). Drafted in the background, polled by the page.
    """

    class Kind(models.TextChoices):
        AUTOMATION = "automation", "Automation"
        TEMPLATE = "template", "Template"
        TEMPLATE_EDIT = "template_edit", "Template edit"
        EMAIL_TEMPLATE = "email_template", "Email template"
        EMAIL_EDIT = "email_edit", "Email edit"
        # Not a setup draft: the owner trying a customer question (apps.ai.drafting).
        PLAYGROUND = "playground", "Try a question"
        KNOWLEDGE_ANSWERS = "knowledge_answers", "Draft knowledge answers"
        WEBSITE = "website", "Read a website"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        READY = "ready", "Ready"
        ERROR = "error", "Error"
        USED = "used", "Used"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="ai_drafts"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    prompt = models.TextField()  # the owner's request, contact details masked
    context = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    result = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error = models.CharField(max_length=300, blank=True, default="")
    model = models.CharField(max_length=80, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"AI draft {self.pk} ({self.kind}, {self.status})"


class AIConversationMemory(models.Model):
    """A rolling summary of the earlier part of one conversation, so AI keeps the thread without
    being sent the whole history.

    The prompt carries the last few messages word for word; everything older is folded into
    ``summary`` and ``facts`` by a background task. ``covered_until_id`` is the newest message
    already folded in. Discardable: delete it and the next refresh rebuilds it.
    """

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="+"
    )
    conversation = models.OneToOneField(
        "conversations.Conversation", on_delete=models.CASCADE, related_name="ai_memory"
    )
    summary = models.TextField(blank=True, default="")
    # Short facts the customer has told the business, e.g. {"wants": "3 kW system", "town": "Kitwe"}.
    facts = models.JSONField(default=dict, blank=True)
    covered_until_id = models.BigIntegerField(default=0)
    model = models.CharField(max_length=80, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AI memory for conversation {self.conversation_id}"
