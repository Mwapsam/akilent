"""A provider that falls back to a second backend when the first one fails.

Any ``AIProviderError`` from the primary (outage, rate limit, timeout, bad key, empty answer) sends
the same conversation to the fallback. The caller's ``timeout`` is not passed on: the fallback is
usually a slower self-hosted model with its own, longer timeout. Log lines carry backend names and
the provider's own (safe) error message only, never customer text or credentials.
"""

from __future__ import annotations

import logging

from apps.ai.providers.base import AIProvider, AIProviderError, CompletionResult

logger = logging.getLogger(__name__)


class FallbackProvider(AIProvider):
    def __init__(
        self,
        primary: AIProvider,
        fallback: AIProvider,
        *,
        primary_name: str = "primary",
        fallback_name: str = "fallback",
    ):
        self.primary = primary
        self.fallback = fallback
        self.primary_name = primary_name
        self.fallback_name = fallback_name

    def chat(
        self,
        messages: list,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.3,
        timeout: float | None = None,
    ) -> CompletionResult:
        try:
            return self.primary.chat(
                messages,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=timeout,
            )
        except AIProviderError as primary_error:
            logger.warning(
                "AI primary %s failed (%s); trying fallback %s.",
                self.primary_name,
                primary_error,
                self.fallback_name,
            )
            try:
                result = self.fallback.chat(
                    messages,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except AIProviderError as fallback_error:
                logger.warning(
                    "AI fallback %s failed too (%s).",
                    self.fallback_name,
                    fallback_error,
                )
                raise AIProviderError(
                    f"{primary_error} Fallback: {fallback_error}"
                ) from fallback_error
        result.provider = f"fallback:{self.fallback_name}"
        return result
