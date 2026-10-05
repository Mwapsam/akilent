from django.db import models


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

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="instagram_comment_triggers",
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    match_type = models.CharField(max_length=20, choices=MatchType.choices)
    keywords = models.TextField(
        blank=True,
        default="",
        help_text="Comma-separated keywords for KEYWORD match type.",
    )

    reply_template = models.TextField(
        help_text="Private reply text. Use {username} to address the commenter."
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

    def keyword_list(self) -> list[str]:
        return [k.strip().lower() for k in self.keywords.split(",") if k.strip()]

    def render_reply(self, username: str = "") -> str:
        return self.reply_template.replace("{username}", username or "there")

    def __str__(self):
        return f"{self.name} ({self.match_type})"
