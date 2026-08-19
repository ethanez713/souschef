"""Logging setup.

Tuned for a small app where the logs are the only debugging tool available.
Three decisions follow from that:

**Everything goes to stdout.** The terminal running the app is the interface;
writing to files would mean hunting for them, and rotating them myself.

**Requests carry an id.** A failure is usually several log lines -- the request,
a warning from the matcher, a traceback -- and without a correlation id they
cannot be tied together after the fact.

**Secrets are never logged.** Not the NYT cookie, not the session token, not the
password. :func:`scrub` exists so that a careless caller still cannot leak them,
because a log file is exactly the place these end up by accident.
"""

from __future__ import annotations

import contextvars
import logging
import re
import sys
import uuid
from typing import Any

REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

# Anything matching these gets redacted before it reaches a handler.
_SENSITIVE_PATTERNS = [
    re.compile(r"(NYT-S=)[^\s;\"']+", re.IGNORECASE),
    re.compile(r"(souschef_session=)[^\s;\"']+", re.IGNORECASE),
    re.compile(r"((?:api[_-]?key|token|secret|password|cookie)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+",
               re.IGNORECASE),
    re.compile(r"(Authorization:\s*\w+\s+)\S+", re.IGNORECASE),
]

REDACTED = r"\1<redacted>"


def scrub(text: str) -> str:
    """Redact anything that looks like a credential."""
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class _RequestIdFilter(logging.Filter):
    """Attach the current request id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = REQUEST_ID.get()
        return True


class _ScrubbingFormatter(logging.Formatter):
    """Format, then redact. Applied last so it also covers exception text."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub(super().format(record))


def configure(level: str | None = None, *, force: bool = False) -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    root = logging.getLogger()
    if root.handlers and not force:
        return
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)

    from souschef.config import get_settings

    resolved = (level or get_settings().log_level).upper()

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        _ScrubbingFormatter(
            fmt="%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )

    root.setLevel(resolved)
    root.addHandler(handler)

    # uvicorn installs its own access log, which duplicates the richer one in
    # the request middleware. Silence it rather than logging every request twice.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("uvicorn.error").propagate = True
    # These are chatty at DEBUG and drown out anything useful.
    for noisy in ("httpx", "httpcore", "PIL", "multipart", "python_multipart"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, getattr(logging, resolved, 20)))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def new_request_id() -> str:
    return uuid.uuid4().hex[:8]


def set_request_id(value: str) -> contextvars.Token:
    return REQUEST_ID.set(value)


def reset_request_id(token: contextvars.Token) -> None:
    REQUEST_ID.reset(token)


def log_startup(settings: Any) -> None:
    """Record the effective configuration once, so a misconfigured run is obvious.

    Deliberately reports only whether each secret is *present* -- the whole
    point of the scrubber is that values like these never reach a log.
    """
    logger = get_logger("souschef.startup")
    logger.info("SousChef starting")
    logger.info("  data dir       : %s", settings.data_dir)
    logger.info("  database       : %s", settings.db_path)
    logger.info("  files          : %s", settings.storage_backend)
    logger.info("  external calls : %s", "BLOCKED" if settings.no_external else "allowed")
    logger.info("  log level      : %s", settings.log_level)

    if not settings.secret_key:
        logger.warning("SOUSCHEF_SECRET_KEY is not set: stored credentials cannot be decrypted")
    if not settings.session_secret:
        logger.error("SOUSCHEF_SESSION_SECRET is not set: sign-in will fail")
    if not settings.spoonacular_key:
        logger.info("  spoonacular    : no key, recipe discovery disabled")
