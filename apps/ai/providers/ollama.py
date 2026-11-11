"""Ollama provider: the hosted Ollama API (ollama.com) or any Ollama server you run.

Plain ``requests`` against ``/api/chat``: no vendor SDK to keep in step. Reasoning models such as
gpt-oss return their thinking in a separate ``message.thinking`` field. Only ``message.content`` is
used, and thinking tokens count towards ``num_predict``, so callers should leave generous headroom
in ``max_tokens``.

A self-hosted server behind a proxy with basic auth takes ``username`` + ``password`` instead of an
API key. ``think`` is sent only when set, so models without a thinking mode are unaffected.

``stream=True`` reads the answer as it is written. Use it for a slow server behind Cloudflare: a
non-streamed answer sends nothing until it is finished, and Cloudflare drops a quiet connection.
``timeout`` is then the cap on the whole answer and ``idle_timeout`` the longest silence allowed.
"""

from __future__ import annotations

from django.conf import settings

from apps.ai.providers.base import AIProvider, AIProviderError, CompletionResult
from apps.ai.providers.http import CONNECT_TIMEOUT, post_json, post_ndjson_stream

DEFAULT_MODEL = "gpt-oss:120b"


class OllamaProvider(AIProvider):
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str | None = None,
        model: str = "",
        timeout: float = 0,
        username: str = "",
        password: str = "",
        think: bool | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        stream: bool = False,
        idle_timeout: float = 90,
    ):
        self.base_url = (
            base_url or getattr(settings, "OLLAMA_BASE_URL", "") or "https://ollama.com"
        ).rstrip("/")
        # None means "use the configured key"; "" means explicitly none (a local server).
        self.api_key = (
            getattr(settings, "OLLAMA_API_KEY", "") if api_key is None else api_key
        )
        self.model = model or getattr(settings, "AI_MODEL", "") or DEFAULT_MODEL
        self.timeout = timeout or getattr(settings, "AI_TIMEOUT_SECONDS", 0) or 45
        self.auth = (username, password) if username and password else None
        self.think = think
        self.connect_timeout = connect_timeout
        self.stream = stream
        self.idle_timeout = idle_timeout

    def chat(
        self,
        messages: list,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.3,
        timeout: float | None = None,
    ) -> CompletionResult:
        wire = ([{"role": "system", "content": system}] if system else []) + [
            {
                "role": m["role"] if isinstance(m, dict) else m.role,
                "content": m["content"] if isinstance(m, dict) else m.content,
            }
            for m in messages
        ]
        headers = {"Content-Type": "application/json"}
        if self.api_key and not self.auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "messages": wire,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if self.think is not None:
            body["think"] = self.think
        if self.stream:
            return self._chat_streamed(body, timeout or self.timeout, headers)
        data = post_json(
            f"{self.base_url}/api/chat",
            headers=headers,
            timeout=timeout or self.timeout,
            model=self.model,
            body=body,
            auth=self.auth,
            connect_timeout=self.connect_timeout,
        )
        text = (
            ((data.get("message") or {}).get("content") or "").strip()
            if isinstance(data.get("message"), dict)
            else ""
        )
        if not text:
            raise AIProviderError("The AI service returned an empty answer.")
        return CompletionResult(
            text=text,
            model=data.get("model") or self.model,
            usage={
                "input_tokens": data.get("prompt_eval_count") or 0,
                "output_tokens": data.get("eval_count") or 0,
            },
        )

    def _chat_streamed(
        self, body: dict, deadline: float, headers: dict
    ) -> CompletionResult:
        parts: list[str] = []
        last: dict = {}
        for chunk in post_ndjson_stream(
            f"{self.base_url}/api/chat",
            headers=headers,
            body={**body, "stream": True},
            deadline=deadline,
            idle_timeout=self.idle_timeout,
            model=self.model,
            auth=self.auth,
            connect_timeout=self.connect_timeout,
        ):
            message = chunk.get("message")
            if isinstance(message, dict):
                parts.append(message.get("content") or "")
            last = chunk
            if chunk.get("done"):
                break
        if not last.get("done"):
            raise AIProviderError("The AI service stopped before finishing its answer.")
        text = "".join(parts).strip()
        if not text:
            raise AIProviderError("The AI service returned an empty answer.")
        return CompletionResult(
            text=text,
            model=last.get("model") or self.model,
            usage={
                "input_tokens": last.get("prompt_eval_count") or 0,
                "output_tokens": last.get("eval_count") or 0,
            },
        )
