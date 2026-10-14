"""package noun and where that package size came from

Revision ID: f6c9d0a41e72
Revises: e5b8c2d14f60
Create Date: 2026-10-14

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6c9d0a41e72"
down_revision: str | Sequence[str] | None = "e5b8c2d14f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("canonical_ingredient") as batch_op:
        batch_op.add_column(sa.Column("package_noun", sa.String(40), nullable=True))
        batch_op.add_column(sa.Column("package_source", sa.String(200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("canonical_ingredient") as batch_op:
        batch_op.drop_column("package_source")
        batch_op.drop_column("package_noun")
