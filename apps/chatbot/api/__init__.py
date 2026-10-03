"""Public API for the chatbot module.

Other apps should import chatbot types from here, not from apps.chatbot.models directly.
"""

from apps.chatbot.models import ChatbotCategory, ChatbotConfig

__all__ = ["ChatbotCategory", "ChatbotConfig"]
