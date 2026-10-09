"""Schema changes apply on a fresh database, not only via the test create_all."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text


def test_head_migration_adds_the_later_columns(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "mig"
    data.mkdir()
    monkeypatch.setenv("SOUSCHEF_DATA_DIR", str(data))

    from souschef.config import get_settings
    from souschef.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    try:
        command.upgrade(Config("alembic.ini"), "head")
        engine = create_engine(f"sqlite+pysqlite:///{data / 'souschef.db'}")
        columns = inspect(engine)
        meal = {column["name"]: column for column in columns.get_columns("meal_plan_entry")}
        shop = {column["name"]: column for column in columns.get_columns("shopping_list_item")}
        recipe = {column["name"]: column for column in columns.get_columns("recipe")}
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        engine.dispose()
    finally:
        reset_engine()
        get_settings.cache_clear()

    assert version == ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    assert recipe["last_servings"]["nullable"] is True
    assert meal["skipped_at"]["nullable"] is True
    assert shop["is_stocked"]["nullable"] is False
    assert shop["is_skipped"]["nullable"] is False


def _at(day: int, hour: int = 12) -> str:
    return f"2026-09-{day:02d} {hour:02d}:00:00.000000"


def _seed_before_beliefs(engine) -> None:  # noqa: ANN001
    stamp = _at(1)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO app_user (id, username, display_name, password_hash, is_admin, "
            "is_active, session_epoch, created_at, updated_at) "
            f"VALUES (1, 'jordan', 'Jordan', 'x', 1, 1, 0, '{stamp}', '{stamp}')"
        ))
        conn.execute(text(
            "INSERT INTO canonical_ingredient (id, name, category, tracking_mode, base_unit, "
            f"is_staple, created_at, updated_at) VALUES (1, 'rice', 'GRAIN', 'BULK', 'g', 0, "
            f"'{stamp}', '{stamp}')"
        ))
        conn.execute(text(
            "INSERT INTO recipe (id, title, source, servings, times_cooked, is_favorite, "
            f"is_archived, created_at, updated_at, user_id) VALUES (1, 'Rice', 'MANUAL', 2, 1, 0, "
            f"0, '{stamp}', '{stamp}', 1)"
        ))
        conn.execute(text(
            "INSERT INTO meal_plan_entry (id, plan_date, slot, servings, is_locked, cooked_at, "
            f"recipe_id, created_at, updated_at, user_id) VALUES (1, '2026-09-02', 'DINNER', 2, 0, "
            f"'{_at(2)}', 1, '{stamp}', '{stamp}', 1)"
        ))
        conn.execute(text(
            "INSERT INTO pantry_item (id, ingredient_id, quantity, confidence, last_confirmed_at, "
            "cooks_since_confirmed, location, created_at, updated_at, user_id) "
            f"VALUES (1, 1, 900, 0.7, '{stamp}', 1, 'PANTRY', '{stamp}', '{stamp}', 1)"
        ))
        events = [
            (1, "PURCHASE", 1000, 1000, 1.0, _at(1), "receipt:1"),
            (2, "PURCHASE", 200, 900, 0.35, _at(2), "recipe:1"),
            (3, "COOK", -300, 900, 1.0, _at(2), "recipe:1"),
        ]
        for event_id, reason, delta, resulting, conf, when, ref in events:
            conn.execute(text(
                "INSERT INTO inventory_event (id, ingredient_id, reason, delta, "
                "resulting_quantity, event_confidence, occurred_at, source_ref, user_id) "
                f"VALUES ({event_id}, 1, '{reason}', {delta}, {resulting}, {conf}, '{when}', "
                f"'{ref}', 1)"
            ))


def test_beliefs_migration_voids_phantom_purchases_and_records_old_cooks(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    data = tmp_path / "mig"
    data.mkdir()
    monkeypatch.setenv("SOUSCHEF_DATA_DIR", str(data))

    from souschef.config import get_settings
    from souschef.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "c3f9a1d2e5b6")
        engine = create_engine(f"sqlite+pysqlite:///{data / 'souschef.db'}")
        _seed_before_beliefs(engine)
        command.upgrade(config, "head")
        with engine.connect() as conn:
            events = {
                row.id: row
                for row in conn.execute(text("SELECT * FROM inventory_event")).mappings()
            }
            cooks = conn.execute(text("SELECT * FROM cook")).mappings().all()
            item = conn.execute(text("SELECT * FROM pantry_item")).mappings().one()
        command.downgrade(config, "c3f9a1d2e5b6")
        restored = {c["name"] for c in inspect(engine).get_columns("pantry_item")}
        engine.dispose()
    finally:
        reset_engine()
        get_settings.cache_clear()

    assert events[2]["voided_at"] is not None
    assert events[1]["voided_at"] is None and events[3]["voided_at"] is None
    assert len(cooks) == 1
    assert (cooks[0]["recipe_id"], cooks[0]["plan_entry_id"], cooks[0]["servings"]) == (1, 1, 2)
    assert events[3]["cook_id"] == cooks[0]["id"] and events[1]["cook_id"] is None
    assert item["quantity"] == 700
    assert item["low"] < 700 < item["high"] == 775
    assert {"confidence", "cooks_since_confirmed"} <= restored
    assert not {"low", "high", "belief_at"} & restored
