"""FastAPI application: middleware, error pages and router wiring."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from souschef import logconfig
from souschef.config import LOOPBACK_HOSTS, ensure_local_secrets, get_settings
from souschef.web.common import (
    WEB_DIR,
    _render,
    log,
    request_hostname,
)
from souschef.web.routes import (
    audit,
    auth,
    cook,
    home,
    meta,
    pantry,
    plan,
    receipts,
    recipes,
    settings,
    shopping,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):  # noqa: ANN201
    ensure_local_secrets()
    logconfig.log_startup(get_settings())
    yield
    log.info("SousChef shutting down")


# openapi_url is disabled alongside the docs UIs: with only those two off,
# /openapi.json still served the full route schema to anonymous callers.
app = FastAPI(title="SousChef", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR.parent / "static")), name="static")


# Requests slower than this are logged as a warning. Everything here is a
# handful of SQLite reads except receipt OCR, so a slow page means something
# is wrong rather than merely busy.
SLOW_REQUEST_SECONDS = 3.0


@app.middleware("http")
async def single_user_guard(request: Request, call_next):  # noqa: ANN001, ANN201
    """No-auth mode answers only this machine.

    A loopback bind behind a reverse proxy such as `tailscale serve` is still
    reachable from the proxy's network, so the Host and forwarding headers are
    checked as well as the bind address.
    """
    if get_settings().single_user:
        forwarded = any(name.startswith("x-forwarded-") for name in request.headers)
        if forwarded or request_hostname(request) not in LOOPBACK_HOSTS:
            return PlainTextResponse("No-auth mode answers only this machine", status_code=403)
    return await call_next(request)


@app.middleware("http")
async def request_logging(request: Request, call_next):  # noqa: ANN001, ANN201
    """Log every request, and make unhandled errors impossible to miss.

    Without this an exception surfaces as a bare 500 in the browser and nothing
    anywhere else. The id is echoed in a response header so a reported failure
    can be found in the log.
    """
    request_id = logconfig.new_request_id()
    token = logconfig.set_request_id(request_id)
    started = time.perf_counter()
    route = f"{request.method} {request.url.path}"

    # The reset has to come after the summary line is emitted, not in a finally
    # that runs before it -- otherwise every request log line is tagged "-" and
    # cannot be correlated with the warnings raised while handling it, which is
    # the entire reason for having an id.
    try:
        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            # exc_info, so the traceback lands in the log, not just the message.
            log.exception("%s failed after %.0fms", route, elapsed)
            raise

        elapsed = (time.perf_counter() - started) * 1000

        if response.status_code >= 500:
            log.error("%s -> %d (%.0fms)", route, response.status_code, elapsed)
        elif response.status_code >= 400:
            log.warning("%s -> %d (%.0fms)", route, response.status_code, elapsed)
        elif elapsed > SLOW_REQUEST_SECONDS * 1000:
            log.warning("%s -> %d SLOW (%.0fms)", route, response.status_code, elapsed)
        else:
            log.info("%s -> %d (%.0fms)", route, response.status_code, elapsed)

        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        logconfig.reset_request_id(token)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN201
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Everything is served from our own origin; no CDNs, so this can be strict.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'self'",
    )
    return response


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


@app.exception_handler(StarletteHTTPException)
async def http_exception_page(request: Request, exc: StarletteHTTPException) -> Response:
    """A browser gets a page. A client that asked only for JSON still gets JSON.

    Routing 404s and 405s raise Starlette's HTTPException, not FastAPI's subclass,
    so the handler has to be registered on the base class or they stay raw JSON.
    Redirects (the signed-out 303) must keep their Location header.
    """
    if exc.status_code in {301, 302, 303, 307, 308}:
        return await http_exception_handler(request, exc)
    if exc.headers and any(key.lower() == "location" for key in exc.headers):
        return await http_exception_handler(request, exc)
    if _wants_json(request):
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) else "Something went wrong."
    titles = {
        404: "Not found",
        403: "Not allowed",
        400: "That didn't work",
        405: "That didn't work",
    }
    return _render(
        request,
        "error.html",
        status_code=exc.status_code,
        title=titles.get(exc.status_code, "Something went wrong"),
        detail=detail,
    )


@app.exception_handler(RequestValidationError)
async def validation_page(request: Request, exc: RequestValidationError) -> Response:
    if _wants_json(request):
        return await request_validation_exception_handler(request, exc)
    return _render(
        request,
        "error.html",
        status_code=422,
        title="That didn't work",
        detail="The form was incomplete or invalid.",
    )


app.include_router(auth.router)
app.include_router(home.router)
app.include_router(pantry.router)
app.include_router(recipes.router)
app.include_router(cook.router)
app.include_router(plan.router)
app.include_router(audit.router)
app.include_router(shopping.router)
app.include_router(receipts.router)
app.include_router(settings.router)
app.include_router(meta.router)
