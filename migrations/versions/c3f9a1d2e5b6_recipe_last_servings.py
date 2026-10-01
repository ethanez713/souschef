"""remember the servings last used for each recipe

Revision ID: c3f9a1d2e5b6
Revises: b7e2c4a91d08
Create Date: 2026-10-01

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f9a1d2e5b6"
down_revision: str | Sequence[str] | None = "b7e2c4a91d08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recipe") as batch_op:
        batch_op.add_column(sa.Column("last_servings", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("recipe") as batch_op:
        batch_op.drop_column("last_servings")
