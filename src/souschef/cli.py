"""Command line entry points for setup and maintenance."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from souschef.config import get_settings


def cmd_init(_args: argparse.Namespace) -> int:
    """Create the schema and load the ingredient catalog."""
    from alembic import command
    from alembic.config import Config

    from souschef.config import REPO_ROOT
    from souschef.core.seeding import seed_ingredients
    from souschef.db import session_scope

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(config, "head")

    with session_scope() as session:
        created, updated, aliases = seed_ingredients(session)
    print(f"Catalog ready: {created} new, {updated} updated, {aliases} aliases added.")
    return 0


def cmd_keygen(_args: argparse.Namespace) -> int:
    """Print the secrets needed to run the app."""
    import secrets

    from souschef.core.crypto import generate_key

    print(f"SOUSCHEF_SECRET_KEY={generate_key()}")
    print(f"SOUSCHEF_SESSION_SECRET={secrets.token_urlsafe(48)}")
    print()
    print("Store these in ~/.secrets/canonical/souschef/ and symlink .secrets to it.")
    print("Losing SECRET_KEY means re-entering the NYT cookie; it decrypts nothing else.")
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


def cmd_rebuild(_args: argparse.Namespace) -> int:
    """Recompute pantry levels by replaying the event ledger."""
    from souschef.core.inventory import rebuild_from_ledger
    from souschef.db import session_scope

    with session_scope() as session:
        count = rebuild_from_ledger(session)
    print(f"Rebuilt {count} pantry rows from the ledger.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "souschef.web.app:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


def cmd_config(_args: argparse.Namespace) -> int:
    settings = get_settings()
    print(f"data dir      : {settings.data_dir}")
    print(f"database      : {settings.db_path}")
    print(f"receipts      : {settings.receipt_dir}")
    print(f"external calls: {'BLOCKED' if settings.no_external else 'allowed'}")
    print(f"secret key    : {'set' if settings.secret_key else 'MISSING'}")
    print(f"session secret: {'set' if settings.session_secret else 'MISSING'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="souschef", description="Pantry-aware meal planner")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and load the catalog").set_defaults(
        func=cmd_init
    )
    sub.add_parser("keygen", help="generate the required secrets").set_defaults(func=cmd_keygen)
    sub.add_parser("rebuild", help="replay the ledger into pantry levels").set_defaults(
        func=cmd_rebuild
    )
    sub.add_parser("config", help="show the effective configuration").set_defaults(
        func=cmd_config
    )

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
