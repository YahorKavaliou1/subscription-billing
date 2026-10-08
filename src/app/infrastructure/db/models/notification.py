import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import NotificationStatus
from app.infrastructure.db.base import Base, TimestampMixin, str_enum


class NotificationModel(TimestampMixin, Base):
    __tablename__ = "notifications"
    __table_args__ = (
        # History of one subscription, ordered by schedule (GET subscription)
        Index("ix_notifications_subscription_id_scheduled_for", "subscription_id", "scheduled_for"),
        # Scheduler picks due notifications; only scheduled rows are indexed
        Index(
            "ix_notifications_due",
            "scheduled_for",
            postgresql_where=text("status = 'scheduled'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    subscription_id: Mapped[str] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"))
    # Set for notifications triggered by a payment (payment status lookup)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"), index=True
    )
    event_name: Mapped[str] = mapped_column(String(64))
    scheduled_for: Mapped[datetime]
    status: Mapped[NotificationStatus] = mapped_column(
        str_enum(NotificationStatus, "notification_status"),
        default=NotificationStatus.SCHEDULED,
        server_default=NotificationStatus.SCHEDULED.value,
    )
    sent_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    # Makes notification creation idempotent, e.g. "<subscription>:<event>:<scheduled_for>"
    dedup_key: Mapped[str] = mapped_column(String(255), unique=True)
