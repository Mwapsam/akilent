from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class SendResult:
    success: bool
    provider_message_id: str = ""
    error: str = ""
    terminal: bool = False  # True = non-retryable (permissions, policy violations)


@dataclass
class EligibilityResult:
    eligible: bool
    reason: str = ""


class BaseInstagramProvider(ABC):
    @abstractmethod
    def can_send(self, recipient_igsid: str, action_type: str) -> EligibilityResult:
        """Check whether a DM or private reply can be sent to this IGSID.

        Meta distinguishes ordinary DM messaging (user must have messaged first,
        within their applicable window) from private replies to comments (7-day
        window from the comment). The provider owns this reasoning.
        """

    @abstractmethod
    def send_message(self, recipient_igsid: str, body: str) -> SendResult:
        """Send a DM via the Instagram Messaging API."""

    @abstractmethod
    def private_reply(self, comment_id: str, body: str) -> SendResult:
        """Send a private reply to a comment (Meta Private Reply API, 7-day window)."""

    @abstractmethod
    def get_user_profile(self, igsid: str) -> dict:
        """Fetch username and name for an IGSID."""

    @abstractmethod
    def hide_comment(self, comment_id: str) -> bool:
        """Hide a comment (reversible). Returns True on success."""

    @abstractmethod
    def delete_comment(self, comment_id: str) -> bool:
        """Permanently delete a comment. Returns True on success."""
