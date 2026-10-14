"""hand-edited shopping amounts survive a rebuild

Revision ID: c4e8a1b72d09
Revises: a8c2e5f19b34
Create Date: 2026-10-14

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1b72d09"
down_revision: str | Sequence[str] | None = "a8c2e5f19b34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("shopping_list_item") as batch_op:
        batch_op.add_column(
            sa.Column(
                "amount_edited", sa.Boolean(), nullable=False, server_default=sa.text("0")
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("shopping_list_item") as batch_op:
        batch_op.drop_column("amount_edited")
