"""Public API for the chatbot module.

Other apps should import chatbot types from here, not from apps.chatbot.models directly.
"""

from apps.chatbot.embed import api_base as widget_api_base
from apps.chatbot.embed import widget_src
from apps.chatbot.models import ChatbotCategory, ChatbotConfig

__all__ = ["ChatbotCategory", "ChatbotConfig", "widget_api_base", "widget_src"]
