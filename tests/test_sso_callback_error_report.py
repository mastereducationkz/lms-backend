"""/auth/sso-callback-error — the report endpoint a failed SSO login uses to say WHY.

The endpoint has to be unauthenticated (a login that died at /auth/callback has no session
yet), so its whole job is to accept untrusted input safely: bound it, refuse to let it shape
the log line, and cap how much of it can be written.
"""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.routes import auth as auth_routes


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/auth")
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_throttle():
    auth_routes._sso_report_window[0] = 0.0
    auth_routes._sso_report_window[1] = 0
    yield
    auth_routes._sso_report_window[0] = 0.0
    auth_routes._sso_report_window[1] = 0


def test_accepts_a_report_and_logs_the_reason(client, caplog):
    with caplog.at_level(logging.WARNING, logger=auth_routes.logger.name):
        resp = client.post(
            "/auth/sso-callback-error",
            json={"reason": "link_expired", "detail": "No matching state found in storage"},
        )
    assert resp.status_code == 204
    assert "reason=link_expired" in caplog.text
    assert "No matching state found in storage" in caplog.text


def test_reason_is_taken_from_a_known_set(client, caplog):
    """An arbitrary `reason` must not end up verbatim in the log line."""
    with caplog.at_level(logging.WARNING, logger=auth_routes.logger.name):
        resp = client.post("/auth/sso-callback-error", json={"reason": "totally-made-up"})
    assert resp.status_code == 204
    assert "reason=other" in caplog.text
    assert "totally-made-up" not in caplog.text


def test_newlines_cannot_forge_extra_log_lines(client, caplog):
    with caplog.at_level(logging.WARNING, logger=auth_routes.logger.name):
        resp = client.post(
            "/auth/sso-callback-error",
            json={"reason": "unknown", "detail": "boom\nWARNING:root:SSO callback failed: reason=idp_rejected"},
        )
    assert resp.status_code == 204
    assert len(caplog.records) == 1
    assert "\n" not in caplog.records[0].getMessage()


def test_long_fields_are_truncated(client, caplog):
    with caplog.at_level(logging.WARNING, logger=auth_routes.logger.name):
        client.post("/auth/sso-callback-error", json={"reason": "unknown", "detail": "x" * 5000})
    assert "x" * 500 in caplog.text
    assert "x" * 501 not in caplog.text


def test_missing_reason_is_rejected(client):
    assert client.post("/auth/sso-callback-error", json={}).status_code == 422


def test_log_volume_is_capped(client, caplog):
    """Unauthenticated + writes a log line = a flood risk. Excess reports are dropped, not written."""
    limit = auth_routes._SSO_REPORT_MAX_PER_MINUTE
    with caplog.at_level(logging.WARNING, logger=auth_routes.logger.name):
        for _ in range(limit + 25):
            assert client.post("/auth/sso-callback-error", json={"reason": "unknown"}).status_code == 204
    assert len(caplog.records) == limit
