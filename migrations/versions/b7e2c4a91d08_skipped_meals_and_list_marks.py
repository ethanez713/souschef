"""skipped meals and shopping-list stock marks

A past plan entry can be skipped without deleting it, and a shopping line can
be marked stocked or skipped so a rebuild of the live list does not undo either.

Revision ID: b7e2c4a91d08
Revises: 8c41f0a9b2d7
Create Date: 2026-09-30

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c4a91d08"
down_revision: str | Sequence[str] | None = "8c41f0a9b2d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("meal_plan_entry") as batch_op:
        batch_op.add_column(sa.Column("skipped_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("shopping_list_item") as batch_op:
        batch_op.add_column(
            sa.Column("is_stocked", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("is_skipped", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )


def downgrade() -> None:
    with op.batch_alter_table("shopping_list_item") as batch_op:
        batch_op.drop_column("is_skipped")
        batch_op.drop_column("is_stocked")
    with op.batch_alter_table("meal_plan_entry") as batch_op:
        batch_op.drop_column("skipped_at")
