"""drop receipt and receipt_line

Revision ID: b1e4c8a07d53
Revises: c4e8a1b72d09
Create Date: 2026-11-02

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1e4c8a07d53"
down_revision: str | Sequence[str] | None = "c4e8a1b72d09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # receipt_line points at receipt. SQLite, with foreign keys on, will not
    # drop a table that is still referenced.
    op.drop_table("receipt_line")
    op.drop_table("receipt")


def downgrade() -> None:
    op.create_table(
        "receipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(length=500), nullable=False),
        sa.Column("image_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "UPLOADED",
                "OCR_RUNNING",
                "NEEDS_REVIEW",
                "APPLIED",
                "FAILED",
                name="receiptstatus",
                native_enum=False,
                length=14,
            ),
            nullable=False,
        ),
        sa.Column(
            "engine",
            sa.Enum(
                "RAPIDOCR",
                "VLM",
                "MANUAL",
                name="ocrengine",
                native_enum=False,
                length=10,
            ),
            nullable=False,
        ),
        sa.Column("store_name", sa.String(length=200), nullable=True),
        sa.Column("purchased_on", sa.Date(), nullable=True),
        sa.Column("total_amount", sa.Float(), nullable=True),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("raw_ocr", sa.Text(), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_receipt_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receipt")),
    )
    with op.batch_alter_table("receipt", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_receipt_image_sha256"), ["image_sha256"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_receipt_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_user_id"), ["user_id"], unique=False)

    op.create_table(
        "receipt_line",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.String(length=300), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=False),
        sa.Column("parsed_description", sa.String(length=200), nullable=True),
        sa.Column("parsed_quantity", sa.Float(), nullable=True),
        sa.Column("parsed_unit", sa.String(length=32), nullable=True),
        sa.Column("parsed_price", sa.Float(), nullable=True),
        sa.Column("ingredient_id", sa.Integer(), nullable=True),
        sa.Column("match_confidence", sa.Float(), nullable=False),
        sa.Column("is_ignored", sa.Boolean(), nullable=False),
        sa.Column("is_confirmed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["canonical_ingredient.id"],
            name=op.f("fk_receipt_line_ingredient_id_canonical_ingredient"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["receipt.id"],
            name=op.f("fk_receipt_line_receipt_id_receipt"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receipt_line")),
    )
    with op.batch_alter_table("receipt_line", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_receipt_line_ingredient_id"), ["ingredient_id"], unique=False
        )
        batch_op.create_index(
            "ix_receipt_line_receipt", ["receipt_id", "position"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_receipt_line_receipt_id"), ["receipt_id"], unique=False
        )
