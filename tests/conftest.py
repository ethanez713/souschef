"""Shared fixtures.

Tests run against a real SQLite file in a temp directory rather than in-memory,
so they exercise the same engine, pragmas and migration path as production.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
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


def _make_user(session: Session, username: str, *, is_admin: bool = False):  # noqa: ANN202
    """Insert an account directly, skipping the argon2 hash.

    Hashing a password costs ~50 ms by design, which is the right price at a
    login form and the wrong one multiplied across a test suite. Tests that
    exercise the login path itself go through the real `auth.create_user`.
    """
    from souschef.models import User

    user = User(
        username=username,
        display_name=username,
        password_hash="not-a-usable-hash",
        is_admin=is_admin,
    )
    session.add(user)
    session.flush()
    return user


@pytest.fixture
def user(session: Session):  # noqa: ANN201
    """The account that owns whatever a test creates."""
    return _make_user(session, "tester", is_admin=True)


@pytest.fixture
def other_user(session: Session):  # noqa: ANN201
    """A second account, for proving one user cannot reach another's data."""
    return _make_user(session, "intruder")


@pytest.fixture
def owner(user) -> int:  # noqa: ANN001
    """Shorthand for the owning user's id, which most core calls take."""
    return user.id


# Credentials for the account that web tests sign in as. Deliberately a
# different username from the `user` fixture, so a test can hold both a
# signed-in account and somebody else's data at the same time.
WEB_USERNAME = "webuser"
WEB_PASSWORD = "a-long-enough-test-password"


@pytest.fixture
def client(seeded_session: Session) -> Iterator[TestClient]:
    """A TestClient against the real app and the real test database."""
    from souschef.web import auth
    from souschef.web.app import app

    # The app opens its own sessions against the same test database.
    seeded_session.commit()

    # The login throttle is in-process, so without this a test that exhausts it
    # locks out every test that runs afterwards -- an order-dependent failure
    # that only shows up when the suite is run as a whole.
    auth._failures.clear()
    yield TestClient(app, follow_redirects=False)
    auth._failures.clear()


@pytest.fixture
def signed_in(client: TestClient) -> TestClient:
    """A client that has claimed the instance and holds a session cookie."""
    response = client.post(
        "/setup",
        data={"username": WEB_USERNAME, "password": WEB_PASSWORD, "confirm": WEB_PASSWORD},
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
def web_user(signed_in: TestClient, session: Session):  # noqa: ANN201, ARG001
    """The account `signed_in` is signed in as, for seeding data it should see."""
    from sqlalchemy import select

    from souschef.models import User

    session.expire_all()
    user = session.scalar(select(User).where(User.username == WEB_USERNAME))
    assert user is not None
    return user


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
