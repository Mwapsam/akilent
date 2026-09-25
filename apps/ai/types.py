"""Typed values shared by every AI provider and by the code that uses them."""
from __future__ import annotations

from dataclasses import dataclass

from apps.ai.providers.base import AIProviderError, CompletionResult

ROLES = ("system", "user", "assistant")


@dataclass(frozen=True)
class ChatMessage:
    """One turn of a conversation sent to a model."""

    role: str
    content: str

    def __post_init__(self):
        if self.role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, not {self.role!r}")


__all__ = ["AIProviderError", "ChatMessage", "CompletionResult", "ROLES"]
