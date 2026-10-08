from datetime import datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import SubscriptionStatus
from app.infrastructure.db.base import Base, TimestampMixin, str_enum


class SubscriptionModel(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (CheckConstraint("day_count > 0", name="day_count_positive"),)

    # External subscription_id, used as the natural primary key
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    day_count: Mapped[int]
    expected_expires_on: Mapped[datetime]
    status: Mapped[SubscriptionStatus] = mapped_column(
        str_enum(SubscriptionStatus, "subscription_status"),
        default=SubscriptionStatus.ACTIVE,
        server_default=SubscriptionStatus.ACTIVE.value,
    )
    # Optimistic locking: concurrent updates of one subscription fail instead of overwriting
    version: Mapped[int] = mapped_column(default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012  # SQLAlchemy API
