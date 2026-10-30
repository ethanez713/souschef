"""Routes: meta."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from souschef.config import get_settings
from souschef.db import get_db
from souschef.models import CanonicalIngredient
from souschef.web.common import (
    STATIC_DIR,
    log,
)

router = APIRouter()


@router.get("/sw.js")
def service_worker() -> FileResponse:
    # A worker served under /static/ cannot control pages. no-cache so a bumped
    # shell is noticed on the next visit.
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "SousChef",
            "short_name": "SousChef",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#F8F6F1",
            "theme_color": "#F8F6F1",
            "icons": [
                {
                    "src": "/static/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/static/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/static/icon-maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
            "share_target": {
                "action": "/share",
                "method": "GET",
                "enctype": "application/x-www-form-urlencoded",
                "params": {"title": "title", "text": "text", "url": "url"},
            },
        },
        media_type="application/manifest+json",
    )


@router.get("/healthz")
def healthz(db: Session = Depends(get_db)) -> JSONResponse:
    """Liveness plus the misconfiguration that breaks stored credentials silently.

    An app that answers 200 while unable to decrypt the stored NYT cookie is
    worse than one that is plainly down.
    """
    problems: list[str] = []
    try:
        db.execute(select(func.count()).select_from(CanonicalIngredient))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        log.exception("Health check: database unreachable")
        problems.append(f"database: {exc}")

    settings = get_settings()
    if not settings.secret_key:
        problems.append("SOUSCHEF_SECRET_KEY is not set")

    if problems:
        log.error("Health check failing: %s", "; ".join(problems))
        return JSONResponse({"status": "degraded", "problems": problems}, status_code=503)
    return JSONResponse({"status": "ok"})
