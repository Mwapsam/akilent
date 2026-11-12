from django.db import models

from .moderation import RuleChannels

# Placeholders a reply can carry besides {username}: the commented post's tracked WhatsApp
# chat link for that channel, and the business's bio page.
LINK_PLACEHOLDERS = ("whatsapp_link", "bio_link")


class CommentTrigger(models.Model):
    """
    A per-account rule that fires a private reply to a commenter.

    When a comment matches a trigger's criteria, Akilent sends a private reply
    DM via the Meta Private Reply API (7-day window from the comment), opening
    an InstagramConversation. The trigger fires at most once per CommentThread
    (enforced via CommentThread.trigger_fired_at).

    Match types:
      keyword       — body contains any word from the keywords list
      buying_intent — detect_buying_intent() returns a match
      any_comment   — fires on every comment (use with caution)

    The reply_template may contain {username} which is replaced at send time.
    """

    class MatchType(models.TextChoices):
        KEYWORD = "keyword", "Keyword / phrase match"
        BUYING_INTENT = "buying_intent", "Buying intent"
        ANY_COMMENT = "any_comment", "Any comment"

    class ReplyType(models.TextChoices):
        DM = "dm", "Private DM (Comment to DM)"
        COMMENT = "comment", "Public comment reply"

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="instagram_comment_triggers",
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    # Which channels' comments the trigger answers (shared with Facebook Page comments).
    channels = models.CharField(
        max_length=10, choices=RuleChannels.choices, default=RuleChannels.BOTH
    )

    match_type = models.CharField(max_length=20, choices=MatchType.choices)
    keywords = models.TextField(
        blank=True,
        default="",
        help_text="Comma-separated keywords for KEYWORD match type.",
    )

    reply_template = models.TextField(
        help_text="Reply text. Use {username} to address the commenter."
    )
    reply_type = models.CharField(
        max_length=10,
        choices=ReplyType.choices,
        default=ReplyType.DM,
        blank=True,
        help_text="How to send the reply: private DM or a public comment reply.",
    )

    # Set when the rule was made for one Content Studio post (its page's "Auto-reply to
    # comments"): it only answers comments on that post, and comes before the general
    # rules there. Null: every post.
    post = models.ForeignKey(
        "publishing.ContentPost",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="comment_triggers",
    )

    # Priority: lower number fires first; only the first matching trigger fires.
    priority = models.PositiveSmallIntegerField(default=10)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["priority", "created_at"]
        indexes = [
            models.Index(fields=["account", "is_active", "priority"]),
        ]

    @property
    def display_status(self) -> dict:
        """Live or Paused, in the shared vocabulary (components/status_badge.html)."""
        if self.is_active:
            return {"key": "live", "label": "Live", "reason": ""}
        return {
            "key": "paused",
            "label": "Paused",
            "reason": "It doesn't act on comments until you switch it on.",
        }

    def keyword_list(self) -> list[str]:
        return [k.strip().lower() for k in self.keywords.split(",") if k.strip()]

    def render_reply(self, username: str = "", links: dict | None = None) -> str:
        """The reply with its placeholders filled.

        ``links``: {"whatsapp_link": url, "bio_link": url} for the commented post
        (apps.instagram.services.triggers.reply_links). A placeholder with no link is
        dropped with the space before it, so the reply never shows "{whatsapp_link}".
        """
        import re

        text = self.reply_template.replace("{username}", username or "there")
        for name in LINK_PLACEHOLDERS:
            url = (links or {}).get(name, "")
            text = (
                text.replace("{" + name + "}", url)
                if url
                else re.sub(r"[ \t]*\{" + name + r"\}", "", text)
            )
        return text.strip()

    def uses_links(self) -> bool:
        return any(
            "{" + name + "}" in self.reply_template for name in LINK_PLACEHOLDERS
        )

    def __str__(self):
        return f"{self.name} ({self.match_type})"
