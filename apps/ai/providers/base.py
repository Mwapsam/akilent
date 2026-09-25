"""Provider-agnostic AIProvider interface.

Any LLM backend (Ollama, Anthropic Claude, OpenAI GPT, a self-hosted model, ...) is supported by
implementing ``chat``. Business logic imports only from here and from ``apps.ai.types``, never from
a concrete provider module, so swapping providers is a configuration change.

Every method returns a typed dataclass, never a raw dict. Adapting a vendor's wire format is the
provider's job and stays inside the provider.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class CompletionResult:
    """Result of an LLM call."""

    text: str
    """The generated text."""

    model: str
    """Model that generated it."""

    usage: dict = None
    """Token usage: {'input_tokens': N, 'output_tokens': M} when the provider reports it."""

    error: Optional[str] = None
    """Error message if the call failed."""

    def __post_init__(self):
        if self.usage is None:
            self.usage = {}


class AIProviderError(Exception):
    """A provider could not produce a result (network, key, rate limit, bad response, timeout).

    The message is safe to log. It never contains the API key or customer text.
    """


class AIProvider(ABC):
    """Abstract interface for an LLM backend.

    Threading: instances are not thread-safe. Instantiate one per request, per Celery task, or per
    service call.
    """

    @abstractmethod
    def chat(
        self,
        messages: list,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.3,
        timeout: Optional[float] = None,
    ) -> CompletionResult:
        """Send a conversation (a list of ``apps.ai.types.ChatMessage``) and return the reply.

        Raises:
            AIProviderError: on network failure, bad credentials, rate limit, timeout, or a
                response that is not usable.
        """

    def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> CompletionResult:
        """Single-prompt convenience wrapper over ``chat``."""
        from apps.ai.types import ChatMessage

        return self.chat(
            [ChatMessage("user", prompt)], system=system, max_tokens=max_tokens, temperature=temperature,
        )

    def health(self) -> CompletionResult:
        """A tiny round trip that proves the key, the network and the model all work."""
        return self.complete("Reply with the single word OK.", max_tokens=64, temperature=0.0)
