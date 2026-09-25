"""Signals the conversation spine sends, for optional consumers (AI today) to react to.

The spine never imports its consumers; they connect in their own ``AppConfig.ready``. A consumer
that fails can never affect recording a message: the spine sends these with ``send_robust``.
"""
from django.dispatch import Signal

# Sent after a customer's message is on the spine and deterministic automations have had their
# chance. Arguments: ``conversation``, ``message`` (the spine Message), ``handled_by_automation``
# (a workflow started or resumed because of it).
conversation_message_processed = Signal()
