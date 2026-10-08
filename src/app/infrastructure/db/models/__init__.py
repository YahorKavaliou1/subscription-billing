"""ORM models. Importing this package registers every table in ``Base.metadata``."""

from app.infrastructure.db.models.inbox import InboxMessageModel
from app.infrastructure.db.models.notification import NotificationModel
from app.infrastructure.db.models.outbox import OutboxEventModel
from app.infrastructure.db.models.payment import PaymentModel
from app.infrastructure.db.models.subscription import SubscriptionModel

__all__ = [
    "InboxMessageModel",
    "NotificationModel",
    "OutboxEventModel",
    "PaymentModel",
    "SubscriptionModel",
]
