"""AI provider factory.

``AI_PROVIDER_BACKEND`` picks the backend: a short alias (below) or a full dotted import path to any
``AIProvider`` subclass. The default, ``none``, means AI is not configured: callers treat that as a
normal state and hide the AI features.

Two model tiers: ``standard`` uses ``AI_MODEL`` (or the provider's default) and ``fast`` uses
``AI_MODEL_FAST`` when it is set, else the standard model. ``apps.ai.router`` picks the tier.
"""

import importlib
import logging

from django.conf import settings

from apps.ai.providers.base import AIProvider, AIProviderError

logger = logging.getLogger(__name__)

_ALIASES: dict[str, str] = {
    "ollama": "apps.ai.providers.ollama.OllamaProvider",
    "anthropic": "apps.ai.providers.anthropic.AnthropicProvider",
    "openai": "apps.ai.providers.openai.OpenAIProvider",
}
TIERS = ("standard", "fast")


def model_for(tier: str = "standard") -> str:
    """The model name to ask for at this tier, or "" to let the provider use its default."""
    if tier == "fast" and (getattr(settings, "AI_MODEL_FAST", "") or "").strip():
        return settings.AI_MODEL_FAST.strip()
    return (getattr(settings, "AI_MODEL", "") or "").strip()


def backend_name() -> str:
    return (getattr(settings, "AI_PROVIDER_BACKEND", "none") or "none").strip()


def is_configured() -> bool:
    """Whether a provider backend is switched on for this site (says nothing about a business)."""
    return backend_name().lower() != "none"


def get_ai_provider(account=None, tier: str = "standard") -> AIProvider:
    """The configured provider at ``tier``. ``account`` is accepted so per-business providers can come later.

    Raises:
        AIProviderError: if AI isn't configured or the backend can't be loaded.
    """
    name = backend_name()
    if name.lower() == "none":
        raise AIProviderError("AI is not configured.")
    path = _ALIASES.get(name.lower(), name)
    try:
        module_name, _, class_name = path.rpartition(".")
        if not module_name:
            raise ImportError(path)
        cls = getattr(importlib.import_module(module_name), class_name)
    except (ImportError, AttributeError) as exc:
        raise AIProviderError(f"Unknown AI backend {name!r}.") from exc
    if not (isinstance(cls, type) and issubclass(cls, AIProvider)):
        raise AIProviderError(f"AI backend {name!r} is not an AIProvider.")
    model = model_for(tier)
    primary = cls(model=model) if model else cls()  # type: ignore[call-arg]
    fallback = get_fallback_provider()
    if fallback is None:
        return primary
    from apps.ai.providers.fallback import FallbackProvider

    return FallbackProvider(
        primary, fallback, primary_name=name, fallback_name=fallback_name()
    )


def fallback_name() -> str:
    return (getattr(settings, "AI_FALLBACK_BACKEND", "") or "").strip().lower()


def fallback_configured() -> bool:
    return bool(fallback_name())


def get_fallback_provider() -> AIProvider | None:
    """The backup provider (a self-hosted Ollama) when ``AI_FALLBACK_BACKEND`` is set, else None.

    Raises:
        AIProviderError: if the fallback backend is set to something unsupported.
    """
    name = fallback_name()
    if not name:
        return None
    if name != "ollama":
        raise AIProviderError(f"Unknown AI fallback backend {name!r}.")
    from apps.ai.providers.ollama import OllamaProvider

    return OllamaProvider(
        base_url=(getattr(settings, "AI_FALLBACK_BASE_URL", "") or "").strip(),
        api_key="",
        model=(getattr(settings, "AI_FALLBACK_MODEL", "") or "").strip(),
        username=(getattr(settings, "AI_FALLBACK_USERNAME", "") or "").strip(),
        password=getattr(settings, "AI_FALLBACK_PASSWORD", "") or "",
        think=False,
        timeout=getattr(settings, "AI_FALLBACK_TIMEOUT_SECONDS", 0) or 180,
        connect_timeout=15,
        # Streamed so Cloudflare (which drops a connection quiet for ~100 s) sees bytes as the
        # model writes. The timeout above caps the whole answer; 90 s is the longest silence.
        stream=True,
        idle_timeout=90,
    )


__all__ = [
    "AIProvider",
    "AIProviderError",
    "backend_name",
    "fallback_configured",
    "fallback_name",
    "get_fallback_provider",
    "get_ai_provider",
    "is_configured",
]
