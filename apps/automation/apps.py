from django.apps import AppConfig


class AutomationConfig(AppConfig):
    name = 'apps.automation'

    def ready(self):
        """Register automation rules as subscribers to domain events.

        This is called once when Django loads the app. It subscribes
        the automation rule engine to domain events published by
        communication modules (WhatsApp, Email, etc.).
        """
        from apps.core.events import BusinessEventReceived, dispatcher, MessageReceived
        from apps.automation import triggers

        # Subscribe to message received events — this is the entry point
        # for the automation WHEN/IF/THEN engine
        dispatcher.subscribe(MessageReceived, triggers.on_message_received)

        # Lifecycle workflows (Phase 6) enrol contacts on business events.
        from apps.automation.workflow_engine import on_business_event

        dispatcher.subscribe(BusinessEventReceived, on_business_event)
