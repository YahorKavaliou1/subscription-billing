"""Shared helpers for SQLAlchemy repositories."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.errors import translate_write_errors


async def flush(session: AsyncSession) -> None:
    """Flush now, translating unique violations into DuplicateKeyError.

    Parent rows are flushed right away: the ORM does not order INSERTs by foreign keys
    without relationship() definitions, and early flushes surface duplicates where they happen.
    """
    async with translate_write_errors():
        await session.flush()
