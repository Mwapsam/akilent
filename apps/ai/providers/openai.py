"""OpenAI provider: the Chat Completions API (``POST /v1/chat/completions``).

Plain ``requests``. Uses ``max_completion_tokens`` and leaves ``temperature`` at the model's default,
because OpenAI's reasoning models reject both ``max_tokens`` and a custom temperature. The model
must be named in ``AI_MODEL``; there's no safe default to guess.

Also works with any OpenAI-compatible server by setting ``OPENAI_BASE_URL``.
"""
from __future__ import annotations

from typing import Optional

from django.conf import settings

from apps.ai.providers.base import AIProvider, AIProviderError, CompletionResult
from apps.ai.providers.http import post_json


class OpenAIProvider(AIProvider):
    def __init__(self, *, base_url: str = "", api_key: str | None = None, model: str = "", timeout: float = 0):
        self.base_url = (base_url or getattr(settings, "OPENAI_BASE_URL", "") or "https://api.openai.com").rstrip("/")
        self.api_key = getattr(settings, "OPENAI_API_KEY", "") if api_key is None else api_key
        self.model = model or getattr(settings, "AI_MODEL", "")
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
            raise AIProviderError("OPENAI_API_KEY is not set.")
        if not self.model:
            raise AIProviderError("Set AI_MODEL to the OpenAI model to use.")
        wire = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m.role, "content": m.content} for m in messages
        ]
        data = post_json(
            f"{self.base_url}/v1/chat/completions", timeout=timeout or self.timeout, model=self.model,
            body={"model": self.model, "messages": wire, "max_completion_tokens": max_tokens},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        choices = data.get("choices") if isinstance(data.get("choices"), list) else []
        message = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
        text = (message.get("content") or "").strip() if isinstance(message.get("content"), str) else ""
        if not text:
            raise AIProviderError("The AI service returned an empty answer.")
        usage = data.get("usage") or {}
        return CompletionResult(
            text=text, model=data.get("model") or self.model,
            usage={"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")},
        )
