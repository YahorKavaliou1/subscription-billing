"""Composition root: wires infrastructure into use cases.

Shared by the API and the workers. Tests build a Container directly with fakes.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sqlalchemy import text

from app.application.ports import Clock, SystemClock, UnitOfWorkFactory
from app.application.use_cases import (
    GetPaymentStatus,
    GetSubscription,
    RegisterPayment,
    UpsertSubscription,
)
from app.config import Settings
from app.domain.policies import NotificationSchedulePolicy
from app.infrastructure.db.session import create_engine, create_session_factory
from app.infrastructure.db.stats import publish_backlog, read_backlog
from app.infrastructure.db.uow import make_uow_factory

HealthCheck = Callable[[], Awaitable[None]]
MetricsRefresher = Callable[[], Awaitable[None]]


@dataclass
class Container:
    api_key: str
    uow_factory: UnitOfWorkFactory
    policy: NotificationSchedulePolicy
    clock: Clock = field(default_factory=SystemClock)
    # name -> check; a check raises if the dependency is unavailable
    readiness_checks: dict[str, HealthCheck] = field(default_factory=dict)
    # Run before every /metrics scrape to update gauges read from the database
    metrics_refreshers: list[MetricsRefresher] = field(default_factory=list)
    shutdown_hooks: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.upsert_subscription = UpsertSubscription(self.uow_factory, self.policy, self.clock)
        self.get_subscription = GetSubscription(self.uow_factory)
        self.register_payment = RegisterPayment(self.uow_factory, self.clock)
        self.get_payment_status = GetPaymentStatus(self.uow_factory)

    async def shutdown(self) -> None:
        for hook in reversed(self.shutdown_hooks):
            await hook()


def build_container(settings: Settings) -> Container:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    clock = SystemClock()

    async def check_database() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def refresh_backlog() -> None:
        publish_backlog(await read_backlog(session_factory), clock.now())

    return Container(
        api_key=settings.api_key.get_secret_value(),
        uow_factory=make_uow_factory(session_factory),
        policy=NotificationSchedulePolicy(settings.notification_offsets_days),
        clock=clock,
        readiness_checks={"database": check_database},
        metrics_refreshers=[refresh_backlog],
        shutdown_hooks=[engine.dispose],
    )
