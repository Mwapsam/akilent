"""Ollama provider: the hosted Ollama API (ollama.com) or any Ollama server you run.

Plain ``requests`` against ``/api/chat``: no vendor SDK to keep in step. Reasoning models such as
gpt-oss return their thinking in a separate ``message.thinking`` field. Only ``message.content`` is
used, and thinking tokens count towards ``num_predict``, so callers should leave generous headroom
in ``max_tokens``.
"""
from __future__ import annotations

from typing import Optional

import requests
from django.conf import settings

from apps.ai.providers.base import AIProvider, AIProviderError, CompletionResult

_CONNECT_TIMEOUT = 5


class OllamaProvider(AIProvider):
    def __init__(self, *, base_url: str = "", api_key: str | None = None, model: str = "", timeout: float = 0):
        self.base_url = (base_url or getattr(settings, "OLLAMA_BASE_URL", "") or "https://ollama.com").rstrip("/")
        # None means "use the configured key"; "" means explicitly none (a local server).
        self.api_key = getattr(settings, "OLLAMA_API_KEY", "") if api_key is None else api_key
        self.model = model or getattr(settings, "AI_MODEL", "") or "gpt-oss:120b"
        self.timeout = timeout or getattr(settings, "AI_TIMEOUT_SECONDS", 0) or 45

    def chat(
        self,
        messages: list,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.3,
        timeout: Optional[float] = None,
    ) -> CompletionResult:
        wire = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m.role, "content": m.content} for m in messages
        ]
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model, "messages": wire, "stream": False,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                },
                headers=headers, timeout=(_CONNECT_TIMEOUT, timeout or self.timeout),
            )
        except requests.Timeout as exc:
            raise AIProviderError("The AI service took too long to answer.") from exc
        except requests.RequestException as exc:
            raise AIProviderError(f"Could not reach the AI service ({type(exc).__name__}).") from exc

        if response.status_code in (401, 403):
            raise AIProviderError("The AI service rejected the API key.")
        if response.status_code == 404:
            raise AIProviderError(f"The AI service doesn't have the model {self.model!r}.")
        if response.status_code == 429:
            raise AIProviderError("The AI service is rate limiting requests.")
        if response.status_code >= 400:
            raise AIProviderError(f"The AI service returned an error ({response.status_code}).")
        try:
            data = response.json()
            text = ((data.get("message") or {}).get("content") or "").strip()
        except (ValueError, AttributeError) as exc:
            raise AIProviderError("The AI service sent a response we couldn't read.") from exc
        if not text:
            raise AIProviderError("The AI service returned an empty answer.")
        return CompletionResult(
            text=text, model=data.get("model") or self.model,
            usage={"input_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")},
        )
