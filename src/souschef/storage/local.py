"""Blobs on the machine's own filesystem."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from souschef.storage import Storage, StorageError, normalize_key


class LocalStorage(Storage):
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def _path(self, key: str) -> Path:
        return self.root / normalize_key(key)

    def write(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written aside and renamed: a crash mid-write leaves the previous blob
        # intact rather than a truncated one that still reads as present.
        staged = path.with_name(f".{path.name}.partial")
        staged.write_bytes(data)
        staged.chmod(0o600)
        staged.replace(path)

    def read(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise StorageError(f"No such stored file: {key}") from exc

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def keys(self, prefix: str = "") -> list[str]:
        base = self.root / prefix if prefix else self.root
        if not base.is_dir():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        )

    @contextmanager
    def local_path(self, key: str) -> Iterator[Path]:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"No such stored file: {key}")
        yield path

    @property
    def location(self) -> str:
        return str(self.root)
