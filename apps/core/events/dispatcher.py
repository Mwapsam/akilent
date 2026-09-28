"""
EventDispatcher abstraction and implementations.

The EventDispatcher is the mechanism by which domain modules publish events
that optional/intelligence services (automation, AI, analytics) subscribe to.

The abstraction lets us swap implementations (Django signals → Redis → Kafka)
without touching any subscriber/publisher code.

KEY INVARIANT: Publisher exceptions must NEVER propagate to subscribers.
If a subscriber fails, it must not break the event-publishing call.
This is what makes "delete all subscribers, core still works" literally true.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable

from django.dispatch import Signal

logger = logging.getLogger(__name__)


class DomainEvent:
    """Base class for all domain events.

    Subclasses should be frozen dataclasses (immutable).
    """

    pass


class EventDispatcher(ABC):
    """Abstraction for publishing and subscribing to domain events."""

    @abstractmethod
    def publish(self, event: DomainEvent) -> None:
        """Publish a domain event to all subscribers.

        Implementation MUST catch and log exceptions from subscribers,
        never let them propagate. If one subscriber fails, others must
        still run and the publisher must not raise.
        """
        pass

    @abstractmethod
    def subscribe(self, event_type: type[DomainEvent], handler: Callable) -> None:
        """Subscribe to a domain event type.

        Args:
            event_type: The event class to subscribe to (e.g. MessageReceived)
            handler: Callable that receives the event instance
        """
        pass


class DjangoSignalDispatcher(EventDispatcher):
    """EventDispatcher backed by Django's built-in Signal mechanism.

    This is suitable for in-process event handling. Signals are NOT
    durable (events are lost if the process dies before they're delivered).
    Use this for development/testing and low-throughput features.

    For production use cases with high volume or cross-process distribution,
    migrate to RedisDispatcher or KafkaDispatcher without changing any
    subscriber/publisher code.
    """

    def __init__(self):
        """Initialize the dispatcher with a signal per event type."""
        self._signals: dict[type[DomainEvent], Signal] = {}

    def _get_signal(self, event_type: type[DomainEvent]) -> Signal:
        """Get or create the signal for an event type."""
        if event_type not in self._signals:
            self._signals[event_type] = Signal()
        return self._signals[event_type]

    def publish(self, event: DomainEvent) -> None:
        """Publish a domain event, catching and logging subscriber exceptions.

        This is the critical point where we enforce the "subscribers must not
        break the publisher" invariant. Each subscriber is called in isolation,
        and exceptions are logged but not propagated.
        """
        event_type = type(event)
        signal = self._get_signal(event_type)

        # Get all connected receivers (subscribers) from the signal
        receivers = signal.receivers or []

        # Django's signal.receivers is a list where each entry is a tuple
        # The second element is the handler function
        for receiver_entry in receivers:
            # receiver_entry is (receiver_key, handler) where handler is a weakref or function
            if len(receiver_entry) >= 2:
                handler_func = receiver_entry[1]
            else:
                continue

            try:
                # Call the handler with the event as the only positional argument
                # The handler signature is handler(event, **kwargs) due to Django signal requirements
                handler_func(event)
            except Exception:
                handler_name = (
                    handler_func.__name__
                    if hasattr(handler_func, "__name__")
                    else str(handler_func)
                )
                logger.exception(
                    "EventDispatcher: subscriber %s raised exception for %s",
                    handler_name,
                    event_type.__name__,
                )
                # Do NOT re-raise — exception is logged and other subscribers still run.

    def subscribe(self, event_type: type[DomainEvent], handler: Callable) -> None:
        """Subscribe to events of a given type.

        The handler will be called with the event instance as its only argument.
        If the handler raises an exception, it will be logged but will not
        affect the publisher or other subscribers.
        """
        signal = self._get_signal(event_type)
        signal.connect(handler, weak=False)
