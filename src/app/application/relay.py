"""Outbox relay: moves committed events from the outbox table to the message broker."""

from dataclasses import dataclass
from datetime import timedelta

from app.application.ports import (
    BrokerUnavailableError,
    Clock,
    EventPublisher,
    UnitOfWorkFactory,
)

MAX_ERROR_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class RelaySettings:
    batch_size: int = 100
    max_attempts: int = 10
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 300.0

    def retry_delay(self, attempts: int) -> timedelta:
        """Exponential backoff after the given number of failed attempts: 1s, 2s, 4s, ... capped."""
        seconds = self.backoff_base_seconds * 2 ** max(attempts - 1, 0)
        return timedelta(seconds=min(seconds, self.backoff_max_seconds))


@dataclass(frozen=True, slots=True)
class RelayResult:
    claimed: int = 0
    published: int = 0
    failed: int = 0
    dead: int = 0
    broker_unavailable: bool = False


class OutboxRelay:
    """One iteration = one transaction:

    1. lock a batch of due pending events (FOR UPDATE SKIP LOCKED);
    2. publish each and wait for the broker's confirmation;
    3. mark it published, or count the failure and schedule a retry;
    4. commit.

    If the process dies after publishing but before commit, the events stay pending
    and are published again: delivery is at-least-once, consumers deduplicate by message_id.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        publisher: EventPublisher,
        clock: Clock,
        settings: RelaySettings | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._publisher = publisher
        self._clock = clock
        self._settings = settings or RelaySettings()

    async def run_once(self) -> RelayResult:
        published = failed = dead = 0
        broker_unavailable = False

        async with self._uow_factory() as uow:
            events = await uow.outbox.claim_pending(self._settings.batch_size, self._clock.now())
            for event in events:
                try:
                    await self._publisher.publish(event)
                except BrokerUnavailableError:
                    # Not the event's fault: keep it pending without counting an attempt.
                    # Events already published in this batch are still marked below.
                    broker_unavailable = True
                    break
                except Exception as exc:  # the broker rejected this particular message
                    attempts = event.attempts + 1
                    give_up = attempts >= self._settings.max_attempts
                    retry_at = (
                        None
                        if give_up
                        else self._clock.now() + self._settings.retry_delay(attempts)
                    )
                    await uow.outbox.mark_failed(event.id, repr(exc)[:MAX_ERROR_LENGTH], retry_at)
                    failed += 1
                    dead += give_up
                else:
                    await uow.outbox.mark_published(event.id, self._clock.now())
                    published += 1
            await uow.commit()

        return RelayResult(
            claimed=len(events),
            published=published,
            failed=failed,
            dead=dead,
            broker_unavailable=broker_unavailable,
        )
