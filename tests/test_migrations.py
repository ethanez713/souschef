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
