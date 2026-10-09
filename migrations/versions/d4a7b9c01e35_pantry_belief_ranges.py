"""pantry levels become ranges folded from the ledger; cooks become records

Revision ID: d4a7b9c01e35
Revises: c3f9a1d2e5b6
Create Date: 2026-10-08

"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "d4a7b9c01e35"
down_revision: str | Sequence[str] | None = "c3f9a1d2e5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pantry_item") as batch_op:
        batch_op.add_column(sa.Column("low", sa.Float(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("high", sa.Float(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("belief_at", sa.DateTime(), nullable=True))
        batch_op.drop_column("confidence")
        batch_op.drop_column("cooks_since_confirmed")

    op.create_table(
        "cook",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("app_user.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("recipe.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("plan_entry_id", sa.Integer(),
                  sa.ForeignKey("meal_plan_entry.id", ondelete="SET NULL"), nullable=True),
        sa.Column("servings", sa.Integer(), nullable=True),
        sa.Column("cooked_at", sa.DateTime(), nullable=False),
        sa.Column("undone_at", sa.DateTime(), nullable=True),
    )

    with op.batch_alter_table("inventory_event") as batch_op:
        batch_op.add_column(
            sa.Column("uncertainty", sa.Float(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("voided_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("cook_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_inventory_event_cook", "cook", ["cook_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_index("ix_inventory_event_cook_id", ["cook_id"])

    _migrate_data(op.get_bind())


def _migrate_data(conn: sa.Connection) -> None:
    """Void the phantom purchases, give old cooks records, and refold every level."""
    now = datetime.now(UTC).replace(tzinfo=None)
    conn.execute(
        sa.text(
            "UPDATE inventory_event SET voided_at = :now "
            "WHERE reason = 'PURCHASE' AND source_ref LIKE 'recipe:%' AND voided_at IS NULL"
        ),
        {"now": _stamp(now)},
    )
    _backfill_cooks(conn)
    _refold_all(conn)


def _backfill_cooks(conn: sa.Connection) -> None:
    groups = conn.execute(
        sa.text(
            "SELECT user_id, source_ref, occurred_at FROM inventory_event "
            "WHERE reason = 'COOK' AND voided_at IS NULL AND source_ref LIKE 'recipe:%' "
            "GROUP BY user_id, source_ref, occurred_at"
        )
    ).all()
    for user_id, source_ref, occurred_at in groups:
        try:
            recipe_id = int(source_ref.removeprefix("recipe:"))
        except ValueError:
            continue
        recipe = conn.execute(
            sa.text("SELECT servings FROM recipe WHERE id = :id AND user_id = :u"),
            {"id": recipe_id, "u": user_id},
        ).first()
        if recipe is None:
            continue
        entry = conn.execute(
            sa.text(
                "SELECT id, servings FROM meal_plan_entry WHERE user_id = :u "
                "AND recipe_id = :r AND cooked_at = :at ORDER BY id LIMIT 1"
            ),
            {"u": user_id, "r": recipe_id, "at": occurred_at},
        ).first()
        cook_id = conn.execute(
            sa.text(
                "INSERT INTO cook (user_id, recipe_id, plan_entry_id, servings, cooked_at) "
                "VALUES (:u, :r, :e, :s, :at)"
            ),
            {
                "u": user_id,
                "r": recipe_id,
                "e": entry[0] if entry else None,
                "s": (entry[1] if entry else None) or recipe[0],
                "at": occurred_at,
            },
        ).lastrowid
        conn.execute(
            sa.text(
                "UPDATE inventory_event SET cook_id = :c WHERE user_id = :u "
                "AND reason = 'COOK' AND source_ref = :ref AND occurred_at = :at"
            ),
            {"c": cook_id, "u": user_id, "ref": source_ref, "at": occurred_at},
        )


def _refold_all(conn: sa.Connection) -> None:
    from souschef.config import get_settings
    from souschef.core.belief import Belief, BeliefEvent, aware, step
    from souschef.models.inventory import EventReason

    settings = get_settings()
    modes = dict(conn.execute(sa.text("SELECT id, tracking_mode FROM canonical_ingredient")).all())

    events = conn.execute(
        sa.text(
            "SELECT id, user_id, ingredient_id, reason, delta, resulting_quantity, "
            "uncertainty, occurred_at FROM inventory_event WHERE voided_at IS NULL"
        )
    ).all()
    by_item: dict[tuple[int, int], list] = defaultdict(list)
    for row in events:
        by_item[(row.user_id, row.ingredient_id)].append(row)

    for user_id, ingredient_id in conn.execute(
        sa.text("SELECT user_id, ingredient_id FROM pantry_item")
    ).all():
        by_item.setdefault((user_id, ingredient_id), [])

    for (user_id, ingredient_id), rows in by_item.items():
        mode = str(modes.get(ingredient_id, "BULK")).upper()
        half_life = (
            settings.confidence_half_life_discrete_days
            if mode == "DISCRETE"
            else settings.confidence_half_life_bulk_days
        )
        belief = Belief()
        for row in sorted(rows, key=lambda r: (aware(_dt(r.occurred_at)), r.id)):
            reason = EventReason[row.reason]
            event = BeliefEvent(
                reason, row.delta, row.resulting_quantity, row.uncertainty or 0.0,
                _dt(row.occurred_at),
            )
            belief = step(
                belief, event, half_life_days=half_life,
                cook_estimate_error=settings.cook_estimate_error,
            )
            if reason not in (EventReason.CONFIRM, EventReason.MANUAL, EventReason.INITIAL):
                conn.execute(
                    sa.text("UPDATE inventory_event SET resulting_quantity = :q WHERE id = :id"),
                    {"q": belief.quantity, "id": row.id},
                )
        params = {
            "q": belief.quantity, "lo": belief.low, "hi": belief.high,
            "at": _stamp(belief.belief_at), "conf": _stamp(belief.last_confirmed_at),
            "u": user_id, "i": ingredient_id,
        }
        if belief.last_confirmed_at is not None:
            conn.execute(
                sa.text(
                    "UPDATE pantry_item SET quantity = :q, low = :lo, high = :hi, "
                    "belief_at = :at, last_confirmed_at = :conf "
                    "WHERE user_id = :u AND ingredient_id = :i"
                ),
                params,
            )
        else:
            conn.execute(
                sa.text(
                    "UPDATE pantry_item SET quantity = :q, low = :lo, high = :hi, "
                    "belief_at = :at WHERE user_id = :u AND ingredient_id = :i"
                ),
                params,
            )


def _stamp(value: datetime | None) -> str | None:
    """SQLite's storage format for DateTime, so raw statements match ORM rows."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def _dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def downgrade() -> None:
    with op.batch_alter_table("inventory_event") as batch_op:
        batch_op.drop_index("ix_inventory_event_cook_id")
        batch_op.drop_constraint("fk_inventory_event_cook", type_="foreignkey")
        batch_op.drop_column("cook_id")
        batch_op.drop_column("voided_at")
        batch_op.drop_column("uncertainty")
    op.drop_table("cook")

    with op.batch_alter_table("pantry_item") as batch_op:
        batch_op.add_column(
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("cooks_since_confirmed", sa.Integer(), nullable=False, server_default="0")
        )
    op.execute("UPDATE pantry_item SET confidence = CASE WHEN high > 0 THEN low / high ELSE 1 END")
    with op.batch_alter_table("pantry_item") as batch_op:
        batch_op.drop_column("belief_at")
        batch_op.drop_column("high")
        batch_op.drop_column("low")
