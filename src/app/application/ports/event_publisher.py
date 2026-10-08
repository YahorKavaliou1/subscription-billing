from typing import Protocol

from app.application.dto import PendingEvent


class BrokerUnavailableError(Exception):
    """The broker cannot be reached at all (connection lost, timeout).

    Not the event's fault: the relay stops the batch and does not count an attempt.
    """


class EventPublisher(Protocol):
    async def publish(self, event: PendingEvent) -> None:
        """Return only after the broker confirmed the message.

        Raises BrokerUnavailableError when the broker is unreachable; any other
        exception means this particular message was rejected.
        """
        ...
