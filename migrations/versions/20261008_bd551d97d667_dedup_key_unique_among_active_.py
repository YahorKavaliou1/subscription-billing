"""dedup key unique among active notifications

Revision ID: bd551d97d667
Revises: fa45d50e3108
Create Date: 2026-10-08 16:40:56.515152+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bd551d97d667"
down_revision: str | None = "fa45d50e3108"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("uq_notifications_dedup_key"), "notifications", type_="unique")
    op.create_index(
        "uq_notifications_dedup_key_active",
        "notifications",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
    )


def downgrade() -> None:
    # Fails if cancelled and active notifications share a key; clean them up first
    op.drop_index(
        "uq_notifications_dedup_key_active",
        table_name="notifications",
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.create_unique_constraint(
        op.f("uq_notifications_dedup_key"),
        "notifications",
        ["dedup_key"],
        postgresql_nulls_not_distinct=False,
    )
