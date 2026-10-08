import asyncio

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infrastructure.db import models  # noqa: F401  # registers all tables
from app.infrastructure.db.base import Base

pytestmark = pytest.mark.integration

APP_TABLES = {"subscriptions", "notifications", "payments", "outbox_events", "inbox_messages"}


def schema_diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )
    return list(compare_metadata(context, Base.metadata))


def table_names(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


async def test_migrations_match_models(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        diff = await connection.run_sync(schema_diff)

    assert diff == [], f"Models and migrations differ, generate a new migration: {diff}"


async def test_downgrade_and_upgrade_are_repeatable(
    engine: AsyncEngine, alembic_config: Config
) -> None:
    # env.py runs its own event loop, so Alembic commands go to a worker thread
    await asyncio.to_thread(command.downgrade, alembic_config, "base")
    async with engine.connect() as connection:
        assert not (await connection.run_sync(table_names)) & APP_TABLES

    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    async with engine.connect() as connection:
        assert await connection.run_sync(table_names) >= APP_TABLES
