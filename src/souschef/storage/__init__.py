"""Where the files this app writes actually live.

The database is deliberately not part of this. SQLite needs byte-range locks and
an fsync that means something, which no cloud drive provides -- so it stays a
local file, and everything else the app writes (receipt images, database
snapshots) goes through the ``Storage`` interface below. Backing the app with
Google Drive is then one class: blobs are stored remotely and the database rides
along as a snapshot pushed by ``souschef backup``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from pathlib import Path

from souschef.config import get_settings


class StorageError(RuntimeError):
    """A backend could not satisfy a read or write."""


def normalize_key(key: str) -> str:
    """Reject anything that could escape the storage root.

    Keys reach here from the database (a receipt row names its own image), so a
    tampered or corrupted row must not be able to address a path outside it.
    """
    parts = key.split("/")
    if not key or key.startswith("/") or "\\" in key or any(p in ("", ".", "..") for p in parts):
        raise StorageError(f"Invalid storage key {key!r}")
    return key


class Storage(ABC):
    """A flat keyspace of blobs, addressed by ``dir/name`` style keys."""

    @abstractmethod
    def write(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def read(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def keys(self, prefix: str = "") -> list[str]:
        """Every key under ``prefix``, sorted."""

    @abstractmethod
    def local_path(self, key: str) -> AbstractContextManager[Path]:
        """Yield a real filesystem path for ``key``, for libraries that need a file.

        Valid only inside the ``with`` block: a remote backend downloads to a
        temporary file and deletes it on exit, so a path kept beyond the block
        points at nothing.
        """

    @property
    @abstractmethod
    def location(self) -> str:
        """Human-readable description, for the startup log and ``souschef config``."""


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        settings = get_settings()
        if settings.storage_backend != "local":
            raise StorageError(
                f"Unknown storage backend {settings.storage_backend!r}. Known backends: local."
            )
        from souschef.storage.local import LocalStorage

        _storage = LocalStorage(settings.data_dir)
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
