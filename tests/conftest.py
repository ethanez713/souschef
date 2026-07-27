"""Shared fixtures.

Tests run against a real SQLite file in a temp directory rather than in-memory,
so they exercise the same engine, pragmas and migration path as production.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session


@pytest.fixture(scope="session", autouse=True)
def _isolate_settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point the app at a throwaway data dir before anything imports settings."""
    data_dir = tmp_path_factory.mktemp("souschef-data")
    os.environ["SOUSCHEF_DATA_DIR"] = str(data_dir)
    os.environ["SOUSCHEF_NO_EXTERNAL"] = "true"
    os.environ["SOUSCHEF_SESSION_SECRET"] = "test-only-not-a-real-secret"
    os.environ["SOUSCHEF_SECRET_KEY"] = "0" * 64

    from souschef.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def _schema(_isolate_settings: None) -> None:
    from souschef.db import get_engine
    from souschef.models import Base

    Base.metadata.create_all(get_engine())


@pytest.fixture
def session(_schema: None) -> Iterator[Session]:
    """A session rolled back after each test, so tests can't leak into each other."""
    from souschef.db import get_engine
    from souschef.models import Base

    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    from souschef.db import get_session_factory

    db = get_session_factory()()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def seeded_session(session: Session) -> Session:
    """A session with the real ingredient catalog loaded.

    Uses the shipped seed file rather than a hand-built fixture: the matcher's
    behaviour depends on the actual catalog vocabulary, so testing against a
    toy catalog would prove nothing about production.
    """
    from souschef.core.seeding import seed_ingredients

    seed_ingredients(session)
    session.commit()
    return session


@pytest.fixture(scope="session")
def seed_path() -> Path:
    from souschef.core.seeding import SEED_PATH

    return SEED_PATH
