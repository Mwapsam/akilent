"""Anthropic provider: Claude through the Messages API (``POST /v1/messages``).

Plain ``requests``, like the Ollama provider. The system prompt is a top-level field, not a message,
and the answer is the text blocks of ``content`` joined together.
"""
from __future__ import annotations

from typing import Optional

from django.conf import settings

from apps.ai.providers.base import AIProvider, AIProviderError, CompletionResult
from apps.ai.providers.http import post_json

DEFAULT_MODEL = "claude-sonnet-5"
API_VERSION = "2023-06-01"


class AnthropicProvider(AIProvider):
    def __init__(self, *, base_url: str = "", api_key: str | None = None, model: str = "", timeout: float = 0):
        self.base_url = (base_url or getattr(settings, "ANTHROPIC_BASE_URL", "") or "https://api.anthropic.com").rstrip("/")
        self.api_key = getattr(settings, "ANTHROPIC_API_KEY", "") if api_key is None else api_key
        self.model = model or getattr(settings, "AI_MODEL", "") or DEFAULT_MODEL
        self.timeout = timeout or getattr(settings, "AI_TIMEOUT_SECONDS", 0) or 45

    def chat(
        self,
        messages: list,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.3,
        timeout: Optional[float] = None,
    ) -> CompletionResult:
        if not self.api_key:
            raise AIProviderError("ANTHROPIC_API_KEY is not set.")
        body = {
            "model": self.model, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": m.role, "content": m.content} for m in messages if m.role != "system"],
        }
        if system:
            body["system"] = system
        data = post_json(
            f"{self.base_url}/v1/messages", timeout=timeout or self.timeout, model=self.model, body=body,
            headers={"Content-Type": "application/json", "x-api-key": self.api_key,
                     "anthropic-version": API_VERSION},
        )
        blocks = data.get("content") if isinstance(data.get("content"), list) else []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
        if not text:
            raise AIProviderError("The AI service returned an empty answer.")
        usage = data.get("usage") or {}
        return CompletionResult(
            text=text, model=data.get("model") or self.model,
            usage={"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens")},
        )
