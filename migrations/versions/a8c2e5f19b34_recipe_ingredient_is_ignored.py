"""ignored recipe lines are not ingredients to name or buy

Revision ID: a8c2e5f19b34
Revises: f6c9d0a41e72
Create Date: 2026-10-14

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c2e5f19b34"
down_revision: str | Sequence[str] | None = "f6c9d0a41e72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recipe_ingredient") as batch_op:
        batch_op.add_column(
            sa.Column("is_ignored", sa.Boolean(), nullable=False, server_default="0")
        )
    _mark_ignored_lines(op.get_bind())


def _mark_ignored_lines(conn: sa.Connection) -> None:
    """Lines already stored that plain water / "to serve" would ignore."""
    from souschef.planning.suggest import is_ignorable

    rows = conn.execute(sa.text("SELECT id, raw_text FROM recipe_ingredient")).all()
    for row in rows:
        if row.raw_text and is_ignorable(row.raw_text):
            conn.execute(
                sa.text("UPDATE recipe_ingredient SET is_ignored = 1 WHERE id = :id"),
                {"id": row.id},
            )


def downgrade() -> None:
    with op.batch_alter_table("recipe_ingredient") as batch_op:
        batch_op.drop_column("is_ignored")
