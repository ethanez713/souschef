"""multi-user accounts and per-user data ownership

Adds ``app_user`` and ``invite``, hangs every user-owned table off an owner, and
adopts whatever is already in the database into a single account so an existing
single-user install keeps all of its data.

The adoption step is the delicate part. The old app stored one password hash in
``app_setting`` under ``auth.password_hash``; that hash becomes the first
account's, so the deployment's existing password still works after upgrading --
only now it is paired with a username.

Revision ID: 8c41f0a9b2d7
Revises: 583397610c73
Create Date: 2026-07-27

"""
from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "8c41f0a9b2d7"
down_revision: str | Sequence[str] | None = "583397610c73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_PASSWORD_KEY = "auth.password_hash"

# Every table that gains an owner, with the indexes/constraints that have to be
# rebuilt around the new column.
OWNED_TABLES = (
    "pantry_item",
    "inventory_event",
    "recipe",
    "meal_plan_entry",
    "shopping_list",
    "receipt",
    "stored_credential",
)


def _existing_row_count(conn: sa.Connection) -> int:
    total = 0
    for table in OWNED_TABLES:
        total += conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
    return total


def _adopt_existing_data(conn: sa.Connection) -> int | None:
    """Create the account that inherits any pre-existing data.

    Returns its id, or None when the database is empty and there is nothing to
    adopt -- in which case the first visitor claims the instance as normal.
    """
    if _existing_row_count(conn) == 0:
        return None

    legacy_hash = conn.execute(
        sa.text("SELECT value FROM app_setting WHERE key = :k"),
        {"k": LEGACY_PASSWORD_KEY},
    ).scalar()

    username = os.environ.get("SOUSCHEF_MIGRATE_USERNAME", "admin").strip().casefold()
    now = datetime.now(UTC)

    if legacy_hash:
        password_hash = legacy_hash
        print(f"Migration: adopting existing data into account {username!r}; "
              "your current password still works.")
    else:
        # Data but no password: the box was set up via the CLI and never had the
        # web UI claimed. Rather than discard the data, park it under an account
        # whose password is unusable and say plainly how to take it back.
        password_hash = "!"  # not a valid argon2 hash; nothing will verify against it
        print(
            f"Migration: found data but no stored password. It now belongs to "
            f"{username!r}, which has NO usable password.\n"
            f"           Set one with:  souschef passwd {username}"
        )

    conn.execute(
        sa.text(
            "INSERT INTO app_user "
            "(username, display_name, password_hash, is_admin, is_active, "
            " session_epoch, created_at, updated_at) "
            "VALUES (:u, :d, :p, 1, 1, 0, :now, :now)"
        ),
        {"u": username, "d": username, "p": password_hash, "now": now},
    )
    return conn.execute(
        sa.text("SELECT id FROM app_user WHERE username = :u"), {"u": username}
    ).scalar_one()


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_epoch", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
    )
    with op.batch_alter_table("app_user", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_app_user_username"), ["username"], unique=True)

    op.create_table(
        "invite",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_by_id", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["app_user.id"],
            name=op.f("fk_invite_created_by_id_app_user"), ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["redeemed_by_id"], ["app_user.id"],
            name=op.f("fk_invite_redeemed_by_id_app_user"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invite")),
    )
    with op.batch_alter_table("invite", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_invite_code_hash"), ["code_hash"], unique=True)
        batch_op.create_index(batch_op.f("ix_invite_created_by_id"), ["created_by_id"])

    conn = op.get_bind()
    owner_id = _adopt_existing_data(conn)

    # Add the column nullable, fill it, then tighten to NOT NULL. Adding it as
    # NOT NULL up front cannot work on a table that already has rows.
    for table in OWNED_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))

        if owner_id is not None:
            conn.execute(
                sa.text(f"UPDATE {table} SET user_id = :uid"), {"uid": owner_id}
            )

        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
            batch_op.create_foreign_key(
                op.f(f"fk_{table}_user_id_app_user"), "app_user",
                ["user_id"], ["id"], ondelete="CASCADE",
            )
            batch_op.create_index(op.f(f"ix_{table}_user_id"), ["user_id"])

    # Uniqueness that used to be global is now per-user.
    with op.batch_alter_table("pantry_item", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pantry_item_ingredient_id"))
        batch_op.create_index(
            batch_op.f("ix_pantry_item_ingredient_id"), ["ingredient_id"], unique=False
        )
        batch_op.create_unique_constraint(
            "uq_pantry_item_user_ingredient", ["user_id", "ingredient_id"]
        )

    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.drop_constraint("uq_recipe_source_external", type_="unique")
        batch_op.create_unique_constraint(
            "uq_recipe_user_source_external", ["user_id", "source", "external_id"]
        )

    with op.batch_alter_table("stored_credential", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("uq_stored_credential_kind"), type_="unique")
        batch_op.drop_index("ix_credential_kind")
        batch_op.create_unique_constraint("uq_credential_user_kind", ["user_id", "kind"])

    # Composite read indexes lead with the owner, since every query filters on it.
    with op.batch_alter_table("inventory_event", schema=None) as batch_op:
        batch_op.drop_index("ix_event_ingredient_time")
        batch_op.create_index(
            "ix_event_user_ingredient_time", ["user_id", "ingredient_id", "occurred_at"]
        )

    with op.batch_alter_table("meal_plan_entry", schema=None) as batch_op:
        batch_op.drop_index("ix_mealplan_date_slot")
        batch_op.create_index(
            "ix_mealplan_user_date_slot", ["user_id", "plan_date", "slot"]
        )


def downgrade() -> None:
    """Return to a single-user schema.

    Destructive by nature: with the owner column gone there is no way to keep
    more than one user's rows, so everything not belonging to the lowest-numbered
    account is deleted rather than silently merged into one incoherent kitchen.
    """
    conn = op.get_bind()
    keep = conn.execute(sa.text("SELECT MIN(id) FROM app_user")).scalar()
    if keep is not None:
        for table in OWNED_TABLES:
            conn.execute(sa.text(f"DELETE FROM {table} WHERE user_id != :k"), {"k": keep})

    with op.batch_alter_table("meal_plan_entry", schema=None) as batch_op:
        batch_op.drop_index("ix_mealplan_user_date_slot")
        batch_op.create_index("ix_mealplan_date_slot", ["plan_date", "slot"])

    with op.batch_alter_table("inventory_event", schema=None) as batch_op:
        batch_op.drop_index("ix_event_user_ingredient_time")
        batch_op.create_index("ix_event_ingredient_time", ["ingredient_id", "occurred_at"])

    with op.batch_alter_table("stored_credential", schema=None) as batch_op:
        batch_op.drop_constraint("uq_credential_user_kind", type_="unique")
        batch_op.create_index("ix_credential_kind", ["kind"])
        batch_op.create_unique_constraint(op.f("uq_stored_credential_kind"), ["kind"])

    with op.batch_alter_table("recipe", schema=None) as batch_op:
        batch_op.drop_constraint("uq_recipe_user_source_external", type_="unique")
        batch_op.create_unique_constraint("uq_recipe_source_external", ["source", "external_id"])

    with op.batch_alter_table("pantry_item", schema=None) as batch_op:
        batch_op.drop_constraint("uq_pantry_item_user_ingredient", type_="unique")
        batch_op.drop_index(batch_op.f("ix_pantry_item_ingredient_id"))
        batch_op.create_index(
            batch_op.f("ix_pantry_item_ingredient_id"), ["ingredient_id"], unique=True
        )

    for table in OWNED_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_index(op.f(f"ix_{table}_user_id"))
            batch_op.drop_constraint(op.f(f"fk_{table}_user_id_app_user"), type_="foreignkey")
            batch_op.drop_column("user_id")

    with op.batch_alter_table("invite", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invite_created_by_id"))
        batch_op.drop_index(batch_op.f("ix_invite_code_hash"))
    op.drop_table("invite")

    with op.batch_alter_table("app_user", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_app_user_username"))
    op.drop_table("app_user")
