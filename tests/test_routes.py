"""The URL surface is a contract: splitting modules must not add, drop or rename a route."""

from __future__ import annotations

from pathlib import Path

from souschef.web.app import app

SNAPSHOT = Path(__file__).parent / "fixtures" / "routes.txt"


def _flatten(routes, prefix: str = ""):  # noqa: ANN001, ANN202
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _flatten(included.routes, prefix + (route.include_context.prefix or ""))
        elif getattr(route, "methods", None) and not route.path.startswith("/_"):
            for method in route.methods:
                yield f"{method} {prefix}{route.path}"


def current_routes() -> list[str]:
    return sorted(set(_flatten(app.routes)))


def test_route_table_matches_the_snapshot() -> None:
    assert current_routes() == SNAPSHOT.read_text().splitlines()
