import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import OutboxStatus
from app.infrastructure.db.base import Base, CreatedAtMixin, str_enum


class OutboxEventModel(CreatedAtMixin, Base):
    """Event written in the same transaction as business data, published later by the relay."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        # Find the event of a given payment / notification (diagnostics endpoint)
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
        # Relay picks events ready to publish; only pending rows are indexed
        Index(
            "ix_outbox_events_pending",
            "available_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    # Also used as message_id in RabbitMQ: consumers deduplicate by it
    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    aggregate_type: Mapped[str] = mapped_column(String(32))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(128))
    routing_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[OutboxStatus] = mapped_column(
        str_enum(OutboxStatus, "outbox_status"),
        default=OutboxStatus.PENDING,
        server_default=OutboxStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    # Next publish attempt not earlier than this moment (retry backoff)
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
