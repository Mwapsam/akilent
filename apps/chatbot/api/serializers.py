"""
Public serialization of ChatbotConfig.

ONLY these fields are exposed to the CDN widget — never account ID, internal
object IDs, knowledge source IDs, action IDs, or provider credentials.
"""

from __future__ import annotations


def public_config(chatbot: object) -> dict:
    return {
        "name": chatbot.name,  # type: ignore[attr-defined]
        "avatar_url": chatbot.avatar_url,  # type: ignore[attr-defined]
        "welcome_message": chatbot.welcome_message,  # type: ignore[attr-defined]
        "primary_color": chatbot.primary_color,  # type: ignore[attr-defined]
        "position": chatbot.position,  # type: ignore[attr-defined]
    }
