"""SQLAlchemy implementations of the persistence ports.

All repositories share one AsyncSession, i.e. one transaction owned by the Unit of Work.
They return domain entities only; ORM models never leave the infrastructure layer.
"""

from app.infrastructure.db.repositories.notifications import SqlNotificationRepository
from app.infrastructure.db.repositories.outbox import SqlOutboxRepository
from app.infrastructure.db.repositories.payments import SqlPaymentRepository
from app.infrastructure.db.repositories.subscriptions import SqlSubscriptionRepository

__all__ = [
    "SqlNotificationRepository",
    "SqlOutboxRepository",
    "SqlPaymentRepository",
    "SqlSubscriptionRepository",
]
