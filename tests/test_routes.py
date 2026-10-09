"""The URL surface is a contract: splitting modules must not add, drop or rename a route."""

from __future__ import annotations

from pathlib import Path

from souschef.web.app import app

SNAPSHOT = Path(__file__).parent / "fixtures" / "routes.txt"


def current_routes() -> list[str]:
    found = {
        f"{method} {route.path}"
        for route in app.routes
        for method in sorted(getattr(route, "methods", None) or [])
    }
    return sorted(found)


def test_route_table_matches_the_snapshot() -> None:
    assert current_routes() == SNAPSHOT.read_text().splitlines()
