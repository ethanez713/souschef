"""Logging tests.

The scrubber is the part that matters most. A log file is exactly where
credentials end up by accident, and on a hosted box those logs are the one
artifact you routinely copy around and paste into issues.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from souschef.logconfig import (
    REQUEST_ID,
    configure,
    get_logger,
    new_request_id,
    reset_request_id,
    scrub,
    set_request_id,
)


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("Cookie: NYT-S=abc123secretvalue; other=1", "abc123secretvalue"),
        ("souschef_session=eyJhbGciOi.signature.here", "eyJhbGciOi.signature.here"),
        ('{"api_key": "sk-live-9f8e7d6c"}', "sk-live-9f8e7d6c"),
        ("password=hunter2hunter2", "hunter2hunter2"),
        ("token: ghp_abcdefghijklmnop", "ghp_abcdefghijklmnop"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1", "eyJhbGciOiJIUzI1"),
        ("secret = topsecretvalue", "topsecretvalue"),
    ],
)
def test_scrub_removes_credentials(raw: str, must_not_contain: str) -> None:
    cleaned = scrub(raw)
    assert must_not_contain not in cleaned
    assert "<redacted>" in cleaned


def test_scrub_leaves_ordinary_text_alone() -> None:
    for text in (
        "OCR receipt.png: 40 boxes -> 14 lines, confidence 0.95",
        "GET /pantry -> 200 (12ms)",
        "Recipe 'Skillet Chicken': 9 ingredients",
    ):
        assert scrub(text) == text


def test_formatter_scrubs_records(caplog: pytest.LogCaptureFixture) -> None:
    """Redaction must survive the whole formatting path, not just direct calls."""
    configure(force=True)
    root = logging.getLogger()
    handler = root.handlers[0]
    record = logging.LogRecord(
        name="test", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="Saving cookie NYT-S=leakedvalue123 now", args=(), exc_info=None,
    )
    record.request_id = "-"
    formatted = handler.format(record)
    assert "leakedvalue123" not in formatted
    assert "<redacted>" in formatted


def test_request_id_round_trips() -> None:
    token = set_request_id("abc12345")
    assert REQUEST_ID.get() == "abc12345"
    reset_request_id(token)
    assert REQUEST_ID.get() == "-"


def test_new_request_ids_are_distinct() -> None:
    assert len({new_request_id() for _ in range(50)}) == 50


def test_configure_is_idempotent() -> None:
    configure(force=True)
    before = len(logging.getLogger().handlers)
    configure()
    configure()
    assert len(logging.getLogger().handlers) == before


def test_warnings_reach_the_log(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="souschef.test"):
        get_logger("souschef.test").warning("something looks wrong: %d", 42)
    assert "something looks wrong: 42" in caplog.text


# ---------------------------------------------------------------- web


@pytest.fixture
def client(seeded_session) -> TestClient:  # noqa: ANN001
    from souschef.web.app import app

    seeded_session.commit()
    return TestClient(app, follow_redirects=False)


def test_responses_carry_a_request_id(client: TestClient) -> None:
    """The header is how a user-reported failure gets found in the log."""
    response = client.get("/healthz")
    assert len(response.headers["X-Request-ID"]) == 8


def test_each_request_gets_its_own_id(client: TestClient) -> None:
    first = client.get("/healthz").headers["X-Request-ID"]
    second = client.get("/healthz").headers["X-Request-ID"]
    assert first != second


def test_requests_are_logged_with_status_and_timing(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="souschef.web"):
        client.get("/healthz")
    assert "GET /healthz -> 200" in caplog.text


def test_request_log_line_carries_the_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: the id was reset in a finally that ran before the log line.

    Every request line came out tagged "-", so a warning raised while handling a
    request could not be tied back to it -- which is the only reason the id
    exists.
    """
    with caplog.at_level(logging.INFO, logger="souschef.web"):
        response = client.get("/healthz")

    summary = next(r for r in caplog.records if "GET /healthz" in r.getMessage())
    assert summary.request_id == response.headers["X-Request-ID"]
    assert summary.request_id != "-"


def test_warnings_during_a_request_share_its_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A warning and the request that caused it must be correlatable."""
    from souschef.web import auth

    auth._failures.clear()
    client.post(
        "/setup", data={"password": "a-long-test-password", "confirm": "a-long-test-password"}
    )
    client.cookies.clear()

    with caplog.at_level(logging.INFO):
        response = client.post("/login", data={"password": "definitely-wrong"})

    request_id = response.headers["X-Request-ID"]
    auth_warning = next(r for r in caplog.records if "Failed sign-in" in r.getMessage())
    assert auth_warning.request_id == request_id
    auth._failures.clear()


def test_unhandled_errors_are_logged_with_a_traceback(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A 500 on a remote box must leave a traceback behind, not just a blank page."""
    from souschef.web.app import app

    @app.get("/_boom_test")
    def _boom() -> None:
        raise RuntimeError("intentional test failure")

    boom_client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="souschef.web"):
        assert boom_client.get("/_boom_test").status_code == 500

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("_boom_test failed" in r.getMessage() for r in errors)
    assert any(r.exc_info for r in errors), "traceback was not captured"
    assert "intentional test failure" in caplog.text


def test_client_errors_are_logged_as_warnings(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # Must be signed in, or the route redirects to /login before it can 404.
    client.post(
        "/setup", data={"password": "a-long-test-password", "confirm": "a-long-test-password"}
    )
    with caplog.at_level(logging.WARNING, logger="souschef.web"):
        assert client.get("/recipes/999999").status_code == 404
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("404" in r.getMessage() for r in warnings)


def test_failed_signin_is_logged(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """On a public box, repeated failures are the signal that matters."""
    from souschef.web import auth

    auth._failures.clear()
    client.post(
        "/setup", data={"password": "a-long-test-password", "confirm": "a-long-test-password"}
    )
    client.cookies.clear()

    with caplog.at_level(logging.WARNING, logger="souschef.auth"):
        client.post("/login", data={"password": "wrong-password-attempt"})
    assert "Failed sign-in" in caplog.text
    # The attempted password must never appear.
    assert "wrong-password-attempt" not in caplog.text
    auth._failures.clear()


def test_lockout_is_logged_as_an_error(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    from souschef.web import auth

    auth._failures.clear()
    client.post(
        "/setup", data={"password": "a-long-test-password", "confirm": "a-long-test-password"}
    )
    client.cookies.clear()

    with caplog.at_level(logging.WARNING, logger="souschef.auth"):
        for _ in range(auth.MAX_ATTEMPTS):
            client.post("/login", data={"password": "nope-nope-nope"})
    assert any(r.levelno == logging.ERROR and "Locked out" in r.getMessage()
               for r in caplog.records)
    auth._failures.clear()


def test_healthz_reports_degraded_when_secrets_are_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container answering 200 while unable to issue sessions is worse than one that is down."""
    from souschef.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "session_secret", "", raising=False)

    response = client.get("/healthz")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "degraded"
    assert any("SESSION_SECRET" in p for p in payload["problems"])


def test_healthz_is_ok_when_configured(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
