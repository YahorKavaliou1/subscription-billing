from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base


class InboxMessageModel(Base):
    """Messages already processed by a consumer: redeliveries are skipped."""

    __tablename__ = "inbox_messages"

    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(server_default=func.now())
