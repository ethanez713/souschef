"""LocalStorage, the only backend today and the shape a cloud one has to match."""

from __future__ import annotations

import pytest

from souschef.storage import StorageError
from souschef.storage.local import LocalStorage


@pytest.fixture
def storage(tmp_path) -> LocalStorage:  # noqa: ANN001
    return LocalStorage(tmp_path / "data")


def test_round_trip(storage: LocalStorage) -> None:
    storage.write("receipts/abc.png", b"pixels")
    assert storage.exists("receipts/abc.png")
    assert storage.read("receipts/abc.png") == b"pixels"

    storage.delete("receipts/abc.png")
    assert not storage.exists("receipts/abc.png")


def test_delete_is_idempotent(storage: LocalStorage) -> None:
    storage.delete("receipts/never-existed.png")


def test_reading_a_missing_key_raises(storage: LocalStorage) -> None:
    with pytest.raises(StorageError):
        storage.read("receipts/missing.png")


def test_local_path_gives_a_real_file(storage: LocalStorage) -> None:
    storage.write("receipts/abc.png", b"pixels")
    with storage.local_path("receipts/abc.png") as path:
        assert path.read_bytes() == b"pixels"

    with pytest.raises(StorageError), storage.local_path("receipts/gone.png"):
        pass


@pytest.mark.parametrize(
    "key",
    ["", "/etc/passwd", "../outside.db", "receipts/../../outside.db", "receipts\\abc.png"],
)
def test_keys_cannot_escape_the_root(storage: LocalStorage, key: str) -> None:
    """A receipt row names its own image, so a bad row must not reach the filesystem."""
    with pytest.raises(StorageError):
        storage.write(key, b"x")
    with pytest.raises(StorageError):
        storage.read(key)


def test_keys_are_listed_sorted_and_scoped_to_the_prefix(storage: LocalStorage) -> None:
    storage.write("backups/souschef-2.db", b"2")
    storage.write("backups/souschef-1.db", b"1")
    storage.write("receipts/abc.png", b"pixels")

    assert storage.keys("backups") == ["backups/souschef-1.db", "backups/souschef-2.db"]
    assert storage.keys("receipts") == ["receipts/abc.png"]
    assert storage.keys() == [
        "backups/souschef-1.db",
        "backups/souschef-2.db",
        "receipts/abc.png",
    ]


def test_listing_an_empty_prefix_is_not_an_error(storage: LocalStorage) -> None:
    assert storage.keys("backups") == []


def test_overwrite_leaves_no_staging_file_behind(storage: LocalStorage) -> None:
    """The write goes via a temp name; a leftover would show up as a phantom key."""
    storage.write("receipts/abc.png", b"first")
    storage.write("receipts/abc.png", b"second")

    assert storage.read("receipts/abc.png") == b"second"
    assert storage.keys("receipts") == ["receipts/abc.png"]
    assert list((storage.root / "receipts").iterdir()) == [storage.root / "receipts/abc.png"]


def test_written_files_are_owner_only(storage: LocalStorage) -> None:
    storage.write("receipts/abc.png", b"pixels")
    assert (storage.root / "receipts/abc.png").stat().st_mode & 0o777 == 0o600
