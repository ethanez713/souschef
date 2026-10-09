"""per-user unit system and the noun a counted ingredient is counted in

Revision ID: e5b8c2d14f60
Revises: d4a7b9c01e35
Create Date: 2026-10-09

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b8c2d14f60"
down_revision: str | Sequence[str] | None = "d4a7b9c01e35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("app_user") as batch_op:
        batch_op.add_column(
            sa.Column("unit_system", sa.String(6), nullable=False, server_default="us")
        )
        batch_op.add_column(
            sa.Column("show_alt_units", sa.Boolean(), nullable=False, server_default="0")
        )
    with op.batch_alter_table("canonical_ingredient") as batch_op:
        batch_op.add_column(sa.Column("count_noun", sa.String(40), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("canonical_ingredient") as batch_op:
        batch_op.drop_column("count_noun")
    with op.batch_alter_table("app_user") as batch_op:
        batch_op.drop_column("show_alt_units")
        batch_op.drop_column("unit_system")
