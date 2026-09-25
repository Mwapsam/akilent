"""AI provider factory.

``AI_PROVIDER_BACKEND`` picks the backend: a short alias (below) or a full dotted import path to any
``AIProvider`` subclass. The default, ``none``, means AI is not configured: callers treat that as a
normal state and hide the AI features. Adding Anthropic or OpenAI later is one class plus one alias.
"""
import importlib
import logging

from django.conf import settings

from apps.ai.providers.base import AIProvider, AIProviderError

logger = logging.getLogger(__name__)

_ALIASES: dict[str, str] = {
    "ollama": "apps.ai.providers.ollama.OllamaProvider",
    # "anthropic": "apps.ai.providers.anthropic.AnthropicProvider",
    # "openai": "apps.ai.providers.openai.OpenAIProvider",
}


def backend_name() -> str:
    return (getattr(settings, "AI_PROVIDER_BACKEND", "none") or "none").strip()


def is_configured() -> bool:
    """Whether a provider backend is switched on for this site (says nothing about a business)."""
    return backend_name().lower() != "none"


def get_ai_provider(account=None) -> AIProvider:
    """The configured provider. ``account`` is accepted so per-business providers can come later.

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
    return cls()


__all__ = ["AIProvider", "AIProviderError", "backend_name", "get_ai_provider", "is_configured"]
