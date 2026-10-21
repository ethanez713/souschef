"""Command line entry points for setup and maintenance."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from souschef.config import get_settings
from souschef.storage import Storage

BACKUP_PREFIX = "backups"


def _snapshots(storage: Storage) -> list[str]:
    return [k for k in storage.keys(BACKUP_PREFIX) if k.endswith(".db")]


def cmd_init(_args: argparse.Namespace) -> int:
    """Create the schema, generate missing keys, and load the ingredient catalog."""
    from alembic import command
    from alembic.config import Config

    from souschef.config import REPO_ROOT, ensure_local_secrets
    from souschef.core.seeding import seed_ingredients
    from souschef.db import session_scope

    settings = get_settings()
    created = ensure_local_secrets()
    if created:
        print(f"Generated {', '.join(created)} in {settings.config_dir}")

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(config, "head")

    with session_scope() as session:
        created, updated, aliases = seed_ingredients(session)  # type: ignore[assignment]
    print(f"Catalog ready: {created} new, {updated} updated, {aliases} aliases added.")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Snapshot the database into the storage backend."""
    import sqlite3
    from datetime import UTC, datetime

    from souschef.storage import get_storage

    settings = get_settings()
    if not settings.db_path.is_file():  # type: ignore[union-attr]
        print(f"No database at {settings.db_path}", file=sys.stderr)
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    staged = settings.db_path.with_name(f".backup-{stamp}.db")  # type: ignore[union-attr]
    # SQLite's own backup API, not a file copy: copying a database that is
    # mid-write produces a torn file that reads fine and restores broken.
    source = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    try:
        target = sqlite3.connect(staged)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    storage = get_storage()
    key = f"{BACKUP_PREFIX}/souschef-{stamp}.db"
    storage.write(key, staged.read_bytes())
    staged.unlink()

    snapshots = _snapshots(storage)
    stale = snapshots[: max(0, len(snapshots) - args.keep)]
    for old in stale:
        storage.delete(old)

    print(f"Saved {key} to {storage.location}")
    if stale:
        print(f"Removed {len(stale)} older snapshot(s), keeping {args.keep}.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Replace the live database with a snapshot. Stop the app first."""
    from souschef.storage import get_storage

    settings = get_settings()
    storage = get_storage()
    snapshots = _snapshots(storage)

    if not args.snapshot:
        if not snapshots:
            print(f"No snapshots in {storage.location}. Run: souschef backup")
            return 1
        print("Snapshots, oldest first:")
        for key in snapshots:
            print(f"  {key}")
        print("\nRestore one with: souschef restore <key>")
        return 0

    key = args.snapshot if "/" in args.snapshot else f"{BACKUP_PREFIX}/{args.snapshot}"
    if key not in snapshots:
        print(f"No such snapshot: {key}", file=sys.stderr)
        return 1

    data = storage.read(key)
    if settings.db_path.is_file():  # type: ignore[union-attr]
        aside = settings.db_path.with_name(  # type: ignore[union-attr]
            settings.db_path.name + ".replaced"  # type: ignore[union-attr]
        )
        settings.db_path.replace(aside)  # type: ignore[union-attr]
        print(f"Previous database moved to {aside}")
    settings.db_path.write_bytes(data)  # type: ignore[union-attr]
    settings.db_path.chmod(0o600)  # type: ignore[union-attr]

    # A leftover write-ahead log belongs to the database that was just replaced;
    # SQLite would replay it into the restored one and corrupt it.
    for suffix in ("-wal", "-shm"):
        settings.db_path.with_name(  # type: ignore[union-attr]
            settings.db_path.name + suffix  # type: ignore[union-attr]
        ).unlink(missing_ok=True)

    print(f"Restored {key}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan a receipt image and print what was found, without touching the pantry."""
    from souschef.db import session_scope
    from souschef.ingest.ocr import run_ocr
    from souschef.ingest.receipt import detect_store, detect_total, parse_receipt

    path = Path(args.image)
    if not path.is_file():
        print(f"No such file: {path}", file=sys.stderr)
        return 1

    result = run_ocr(path)
    with session_scope() as session:
        lines = parse_receipt(result, session)

        print(f"Store: {detect_store(result.lines) or 'unknown'}")
        print(f"Total: {detect_total(result.lines) or 'unknown'}")
        print(f"Scan quality: {result.mean_confidence:.0%}\n")

        for line in lines:
            if line.is_ignored if hasattr(line, "is_ignored") else line.is_noise:
                continue
            name = line.match.name if line.match and line.match.is_match else "NO MATCH"
            flag = "ok  " if line.match and line.match.is_auto else "check"
            amount = f"{line.quantity:g} {line.unit}" if line.quantity else "-"
            print(f"  [{flag}] {amount:>12}  {line.raw_text[:38]:40} -> {name}")
    return 0


def cmd_rebuild(args: argparse.Namespace) -> int:
    """Recompute pantry levels by replaying the event ledger."""
    from sqlalchemy import select

    from souschef.core.inventory import rebuild_from_ledger
    from souschef.db import session_scope
    from souschef.models import User

    with session_scope() as session:
        users = session.scalars(select(User).order_by(User.id)).all()
        if args.user:
            users = [u for u in users if u.username == args.user.strip().casefold()]
            if not users:
                print(f"No such user: {args.user}", file=sys.stderr)
                return 1

        total = 0
        for user in users:
            count = rebuild_from_ledger(session, user.id)
            print(f"  {user.username}: {count} pantry rows")
            total += count

    print(f"Rebuilt {total} pantry rows from the ledger.")
    return 0


def cmd_users(_args: argparse.Namespace) -> int:
    """List accounts."""
    from sqlalchemy import select

    from souschef.db import session_scope
    from souschef.models import User

    with session_scope() as session:
        users = session.scalars(select(User).order_by(User.id)).all()
        if not users:
            print("No accounts yet.")
            return 0
        for user in users:
            flags = " ".join(
                f for f in ("admin" if user.is_admin else "", "" if user.is_active else "disabled")
                if f
            )
            print(f"  {user.id:>3}  {user.username:<24} {flags}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from souschef.config import LOOPBACK_HOSTS

    if args.host not in LOOPBACK_HOSTS:
        print(
            "there is no sign-in, reach it from other devices via `tailscale serve`",
            file=sys.stderr,
        )
        return 2

    # The app already logs method and path. Uvicorn's access line includes the
    # query string, which is where a shared recipe URL sits.
    uvicorn.run(
        "souschef.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=False,
    )
    return 0


def cmd_config(_args: argparse.Namespace) -> int:
    from souschef.storage import get_storage

    settings = get_settings()
    print(f"data dir      : {settings.data_dir}")
    print(f"config dir    : {settings.config_dir}")
    print(f"database      : {settings.db_path}")
    print(f"files         : {settings.storage_backend} -> {get_storage().location}")
    print(f"external calls: {'BLOCKED' if settings.no_external else 'allowed'}")
    print(f"secret key    : {'set' if settings.secret_key else 'MISSING'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="souschef", description="Pantry-aware meal planner")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and load the catalog").set_defaults(
        func=cmd_init
    )
    sub.add_parser("config", help="show the effective configuration").set_defaults(
        func=cmd_config
    )
    sub.add_parser("users", help="list accounts").set_defaults(func=cmd_users)

    rebuild = sub.add_parser("rebuild", help="replay the ledger into pantry levels")
    rebuild.add_argument("--user", default=None, help="one username (default: everyone)")
    rebuild.set_defaults(func=cmd_rebuild)

    backup = sub.add_parser("backup", help="snapshot the database")
    backup.add_argument("--keep", type=int, default=14, help="snapshots to retain (default 14)")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="restore a snapshot (list them with no argument)")
    restore.add_argument("snapshot", nargs="?", default="")
    restore.set_defaults(func=cmd_restore)

    scan = sub.add_parser("scan", help="dry-run OCR on a receipt image")
    scan.add_argument("image")
    scan.set_defaults(func=cmd_scan)

    serve = sub.add_parser("serve", help="run the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    parser.add_argument(
        "--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR (default from env)"
    )

    args = parser.parse_args(argv)

    from souschef.logconfig import configure

    configure(args.log_level, force=True)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
