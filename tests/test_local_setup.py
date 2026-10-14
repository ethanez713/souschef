"""First-run behaviour of a local install: its own keys, and its own backups."""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator

import pytest

from souschef import config as config_module
from souschef import storage as storage_module
from souschef.cli import cmd_backup, cmd_restore
from souschef.config import Settings, ensure_local_secrets


@pytest.fixture
def install(tmp_path, monkeypatch) -> Iterator[Settings]:  # noqa: ANN001
    """A throwaway install with nothing supplied by the environment."""
    monkeypatch.setenv("SOUSCHEF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOUSCHEF_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("SOUSCHEF_SESSION_SECRET", raising=False)
    monkeypatch.delenv("SOUSCHEF_SECRET_KEY", raising=False)
    config_module.get_settings.cache_clear()
    storage_module.reset_storage()
    yield config_module.get_settings()
    config_module.get_settings.cache_clear()
    storage_module.reset_storage()


def test_first_run_generates_its_own_keys(install: Settings) -> None:
    assert ensure_local_secrets() == ["secret_key"]

    assert len(install.secret_key) == 64  # 32 bytes hex, what AES-GCM wants
    assert (install.config_dir / "secret_key").stat().st_mode & 0o777 == 0o600
    assert install.config_dir.stat().st_mode & 0o777 == 0o700
    assert not (install.config_dir / "session_secret").exists()


def test_keys_survive_a_restart(install: Settings) -> None:
    """Regenerating on every start would orphan the NYT cookie."""
    ensure_local_secrets()

    restarted = Settings()
    assert restarted.secret_key == install.secret_key
    assert ensure_local_secrets() == []


def test_an_existing_session_secret_file_still_loads(install: Settings) -> None:
    path = install.config_dir / "session_secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("already-there\n", encoding="utf-8")
    config_module.get_settings.cache_clear()
    try:
        loaded = Settings()
        assert loaded.session_secret == "already-there"
        assert ensure_local_secrets() == ["secret_key"]
        assert loaded.session_secret == "already-there"
    finally:
        config_module.get_settings.cache_clear()


def test_environment_wins_over_generated_keys(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("SOUSCHEF_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SOUSCHEF_SESSION_SECRET", "from-the-environment")
    monkeypatch.delenv("SOUSCHEF_SECRET_KEY", raising=False)
    config_module.get_settings.cache_clear()
    try:
        settings = config_module.get_settings()
        assert ensure_local_secrets() == ["secret_key"]
        assert settings.session_secret == "from-the-environment"
        assert not (settings.config_dir / "session_secret").exists()
    finally:
        config_module.get_settings.cache_clear()


def _seed_database(install: Settings, note: str) -> None:
    install.db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(install.db_path)
    with connection:
        connection.execute("CREATE TABLE IF NOT EXISTS note (text TEXT)")
        connection.execute("DELETE FROM note")
        connection.execute("INSERT INTO note VALUES (?)", (note,))
    connection.close()


def _read_note(install: Settings) -> str:
    connection = sqlite3.connect(install.db_path)
    try:
        return connection.execute("SELECT text FROM note").fetchone()[0]
    finally:
        connection.close()


def test_backup_then_restore_recovers_the_database(install: Settings) -> None:
    _seed_database(install, "before")
    assert cmd_backup(argparse.Namespace(keep=14)) == 0

    snapshots = storage_module.get_storage().keys("backups")
    assert len(snapshots) == 1

    _seed_database(install, "after")
    assert cmd_restore(argparse.Namespace(snapshot=snapshots[0])) == 0
    assert _read_note(install) == "before"


def test_restore_clears_the_write_ahead_log(install: Settings) -> None:
    """A stale -wal belongs to the replaced database and would corrupt the restored one."""
    _seed_database(install, "before")
    cmd_backup(argparse.Namespace(keep=14))
    wal = install.db_path.with_name(install.db_path.name + "-wal")
    wal.write_bytes(b"stale")

    snapshots = storage_module.get_storage().keys("backups")
    assert cmd_restore(argparse.Namespace(snapshot=snapshots[0])) == 0
    assert not wal.exists()


def test_backup_prunes_the_oldest_snapshots(install: Settings) -> None:
    _seed_database(install, "before")
    storage = storage_module.get_storage()
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        storage.write(f"backups/souschef-{stamp}.db", b"old")

    assert cmd_backup(argparse.Namespace(keep=2)) == 0

    remaining = storage.keys("backups")
    assert len(remaining) == 2
    assert "backups/souschef-20260101T000000Z.db" not in remaining


def test_restore_refuses_an_unknown_snapshot(install: Settings) -> None:
    _seed_database(install, "before")
    assert cmd_restore(argparse.Namespace(snapshot="backups/nope.db")) == 1
    assert _read_note(install) == "before"


def test_serve_refuses_a_non_loopback_host(monkeypatch, capsys) -> None:  # noqa: ANN001
    import uvicorn

    from souschef.cli import main

    started: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append((a, k)))

    assert main(["serve", "--host", "0.0.0.0"]) == 2
    assert started == []
    err = capsys.readouterr().err
    assert "there is no sign-in, reach it from other devices via `tailscale serve`" in err


def test_serve_on_loopback_starts(monkeypatch) -> None:  # noqa: ANN001
    import uvicorn

    from souschef.cli import main

    started: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append((a, k)))

    assert main(["serve"]) == 0
    assert len(started) == 1
    assert started[0][1]["host"] == "127.0.0.1"
