"""Warnings about comment rules that overlap, never fire, or catch far too much.

Both kinds of comment rule are **first-match**: for a comment, only the first active rule
(by priority, then age) that matches acts (``moderation.first_matching_rule``,
``triggers.first_matching_trigger``). So a rule whose matches are all caught by an
earlier rule never acts at all, and two rules doing the same job are clutter at best.

Keyword matching is a case-insensitive *substring* test, so a short keyword like "hi"
also matches inside "this" and "which". That is the main way a rule ends up catching
almost every comment.

Nothing here changes a rule: the page shows the warnings and the owner decides (turn one
off, or merge it into another with a preview first).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.instagram.models import CommentTrigger, ModerationRule
from apps.instagram.models.moderation import RuleChannels

# Words so common in comments that a rule keyed on them acts on nearly everyone.
# High-risk rules (hide/delete a comment, or answer it automatically) are warned about
# all of them; rules that only flag, email or open a lead about the very generic ones.
BROAD_FOR_RISKY = {
    "service",
    "help",
    "link",
    "contact",
    "info",
    "hi",
    "hello",
    "hey",
    "please",
    "thanks",
    "thank you",
    "nice",
    "love",
    "great",
    "yes",
    "ok",
    "dm",
    "message",
    "details",
    "price",
    "cost",
    "how much",
}
BROAD_FOR_ANY = {
    "service",
    "help",
    "link",
    "contact",
    "info",
    "hi",
    "hello",
    "hey",
    "please",
}
SHORT_KEYWORD = 3  # this many letters or fewer also match inside longer words

# Common words to show where a short keyword also matches ("hi" -> "this").
_SAMPLE_WORDS = (
    "this",
    "which",
    "nothing",
    "shipping",
    "think",
    "other",
    "there",
    "admit",
    "order",
    "showing",
    "anyone",
    "contact",
    "whatsapp",
    "beautiful",
    "linked",
    "helpful",
)


@dataclass
class RuleWarning:
    kind: str  # "shadowed" | "overlap" | "broad"
    severity: str  # "warning" | "note"
    message: str
    rule_kind: str  # "moderation" | "reply"
    rule_pk: int
    other_pk: int | None = None  # the rule it overlaps with (merge target)
    other_name: str = ""


@dataclass
class LintResult:
    warnings: list[RuleWarning] = field(default_factory=list)

    def for_rule(self, rule_kind: str, pk: int) -> list[RuleWarning]:
        return [
            w for w in self.warnings if w.rule_kind == rule_kind and w.rule_pk == pk
        ]

    @property
    def page_level(self) -> list[RuleWarning]:
        """The ones worth a banner: shadowed and overlapping rules, and risky broad ones."""
        return [w for w in self.warnings if w.severity == "warning"]


def _channels(rule) -> set[str]:
    return (
        {"instagram", "facebook"}
        if rule.channels == RuleChannels.BOTH
        else {rule.channels}
    )


def _channel_words(channels: set[str]) -> str:
    if channels == {"instagram", "facebook"}:
        return "Instagram and Facebook"
    return " and ".join(sorted(c.capitalize() for c in channels))


def _patterns(rule) -> list:
    """The built-in patterns a moderation rule's type also matches on."""
    if not isinstance(rule, ModerationRule):
        return []
    from apps.instagram.services.moderation import _COMPILED

    return _COMPILED.get(rule.match_type, [])


def _covered(keyword: str, earlier) -> bool:
    """Whether every comment containing ``keyword`` is also caught by ``earlier``:
    one of its keywords is inside ``keyword``, or its built-in patterns match it."""
    return any(other in keyword for other in earlier.keyword_list()) or any(
        p.search(keyword) for p in _patterns(earlier)
    )


def _has_patterns(rule) -> bool:
    """Rules whose type matches on built-in patterns as well as keywords."""
    if isinstance(rule, ModerationRule):
        return rule.match_type != ModerationRule.MatchType.KEYWORD
    return rule.match_type != CommentTrigger.MatchType.KEYWORD


def _is_risky(rule) -> bool:
    if isinstance(rule, CommentTrigger):
        return True  # it answers people automatically
    return rule.moderation_action in (
        ModerationRule.ModerationAction.HIDE,
        ModerationRule.ModerationAction.DELETE,
    )


def _shadow_or_overlap(earlier, later) -> str:
    """ "shadowed" if ``earlier`` catches every comment ``later`` would, "overlap" if
    they share a good part of the same job, else ""."""
    shared = _channels(earlier) & _channels(later)
    if not shared:
        return ""
    if (
        isinstance(later, CommentTrigger)
        and later.match_type == CommentTrigger.MatchType.ANY_COMMENT
    ):
        if (
            isinstance(earlier, CommentTrigger)
            and earlier.match_type == CommentTrigger.MatchType.ANY_COMMENT
        ):
            return "shadowed" if _channels(earlier) >= _channels(later) else "overlap"
        return ""
    if (
        isinstance(earlier, CommentTrigger)
        and earlier.match_type == CommentTrigger.MatchType.ANY_COMMENT
    ):
        return "shadowed" if _channels(earlier) >= _channels(later) else "overlap"

    later_kw = later.keyword_list()
    same_type = earlier.match_type == later.match_type
    patterns_covered = not _has_patterns(later) or same_type
    keywords_covered = all(_covered(k, earlier) for k in later_kw)
    if (
        _channels(earlier) >= _channels(later)
        and patterns_covered
        and keywords_covered
        and (later_kw or _has_patterns(later))
    ):
        return "shadowed"
    if same_type and _has_patterns(later):
        return "overlap"
    if later_kw:
        share = sum(_covered(k, earlier) for k in later_kw) / len(later_kw)
        if share >= 0.5:
            return "overlap"
    earlier_kw = earlier.keyword_list()
    if earlier_kw and _patterns(later):
        # The earlier rule's words are what the later rule's built-in patterns look for
        # ("Complaint keywords" before "Complaints"): the same job, done twice.
        share = sum(any(p.search(k) for p in _patterns(later)) for k in earlier_kw)
        if share / len(earlier_kw) >= 0.5:
            return "overlap"
    return ""


def _pairs(rules: list, rule_kind: str, noun: str) -> list[RuleWarning]:
    warnings = []
    active = [r for r in rules if r.is_active]
    for i, later in enumerate(active):
        for earlier in active[:i]:
            verdict = _shadow_or_overlap(earlier, later)
            if not verdict:
                continue
            where = _channel_words(_channels(earlier) & _channels(later))
            if verdict == "shadowed":
                message = (
                    f"Never acts: “{earlier.name}” comes first and catches every {where} "
                    f"comment this {noun} would. Only the first matching {noun} acts on a comment."
                )
            else:
                message = (
                    f"Overlaps with “{earlier.name}”, which comes first, on {where} comments. "
                    f"Only the first matching {noun} acts, so this one misses those comments."
                )
            warnings.append(
                RuleWarning(
                    kind=verdict,
                    severity="warning",
                    message=message,
                    rule_kind=rule_kind,
                    rule_pk=later.pk,
                    other_pk=earlier.pk,
                    other_name=earlier.name,
                )
            )
            break  # one overlap per rule is enough to act on
    return warnings


def _broad(rule, rule_kind: str) -> list[RuleWarning]:
    risky = _is_risky(rule)
    listed = BROAD_FOR_RISKY if risky else BROAD_FOR_ANY
    common = [k for k in rule.keyword_list() if k in listed]
    short = [k for k in rule.keyword_list() if len(k) <= SHORT_KEYWORD]
    if not common and not short:
        return []
    parts = []
    if common:
        quoted = ", ".join(f"“{k}”" for k in common[:4])
        parts.append(f"{quoted} {'is' if len(common) == 1 else 'are'} in most comments")
    for k in short[:2]:
        inside = [w for w in _SAMPLE_WORDS if k in w and w != k][:2]
        if inside:
            parts.append(
                f"“{k}” also matches inside words like {' and '.join(f'“{w}”' for w in inside)}"
            )
        else:
            parts.append(f"“{k}” is short enough to match inside other words")
    # A rule that opens a lead or emails the team on nearly every comment buries the real
    # ones (and fills Customers with people who only said hello): warned, not just noted.
    acts = isinstance(rule, ModerationRule) and rule.automation_trigger in (
        ModerationRule.AutomationTrigger.CREATE_PROPOSAL,
        ModerationRule.AutomationTrigger.NOTIFY_STAFF,
    )
    if not risky and acts and common:
        opens = (
            "open a lead"
            if rule.automation_trigger
            == ModerationRule.AutomationTrigger.CREATE_PROPOSAL
            else "email your team"
        )
        message = (
            f"Catches too much: {'; '.join(parts)}, so it would {opens} for most "
            "comments. Use phrases people only write when they mean it, like "
            "“how much is” or “do you deliver”."
        )
        return [
            RuleWarning(
                kind="broad",
                severity="warning",
                message=message,
                rule_kind=rule_kind,
                rule_pk=rule.pk,
            )
        ]
    if risky:
        what = (
            "it would answer almost every comment automatically"
            if isinstance(rule, CommentTrigger)
            else f"it would {rule.get_moderation_action_display().split()[0].lower()} real customers' comments"
        )
        message = (
            f"Catches too much: {'; '.join(parts)}, so {what}. Use longer phrases."
        )
        severity = "warning"
    else:
        message = (
            f"May tag most comments: {'; '.join(parts)}. Longer phrases keep it useful."
        )
        severity = "note"
    return [
        RuleWarning(
            kind="broad",
            severity=severity,
            message=message,
            rule_kind=rule_kind,
            rule_pk=rule.pk,
        )
    ]


def lint_rules(account) -> LintResult:
    """Every warning for the account's comment rules (both kinds)."""
    moderation = list(
        ModerationRule.objects.filter(account=account).order_by(
            "priority", "created_at"
        )
    )
    triggers = list(
        CommentTrigger.objects.filter(account=account).order_by(
            "priority", "created_at"
        )
    )
    result = LintResult()
    result.warnings += _pairs(moderation, "moderation", "rule")
    # A rule made for one post fires first on that post and nowhere else, so it neither
    # hides a general rule nor is hidden by one: only the general rules are compared.
    result.warnings += _pairs(
        [t for t in triggers if t.post_id is None], "reply", "reply rule"
    )
    for rule in moderation:
        if rule.is_active:
            result.warnings += _broad(rule, "moderation")
    for trigger in triggers:
        if trigger.is_active:
            result.warnings += _broad(trigger, "reply")
    return result


def merge_preview(source: ModerationRule, target: ModerationRule) -> dict:
    """What merging ``source`` into ``target`` would change: target gains source's
    keywords and channels; source is turned off (never deleted)."""
    keywords = list(target.keywords_as_entered())
    seen = {k.lower() for k in keywords}
    for k in source.keywords_as_entered():
        if k.lower() not in seen:
            keywords.append(k)
            seen.add(k.lower())
    channels = (
        target.channels if target.channels == source.channels else RuleChannels.BOTH
    )
    return {
        "keywords": ", ".join(keywords),
        "added_keywords": [
            k for k in keywords if k not in target.keywords_as_entered()
        ],
        "channels": channels,
        "channels_label": RuleChannels(channels).label,
        "channels_changed": channels != target.channels,
    }


def apply_merge(source: ModerationRule, target: ModerationRule) -> dict:
    preview = merge_preview(source, target)
    target.keywords = preview["keywords"]
    target.channels = preview["channels"]
    target.save(update_fields=["keywords", "channels", "updated_at"])
    source.is_active = False
    source.save(update_fields=["is_active", "updated_at"])
    return preview
