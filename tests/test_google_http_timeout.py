"""Every Google API client is built on an HTTP transport with a deadline.

2026-09-15: one Meet response never arrived, httplib2 waited without a timeout, and the recordings
worker sat in poll() for over an hour — no attendance, no recordings, no rooms — until a restart.
"""
from src.services import google_workspace


class _Creds:
    """Stands in for OAuth credentials; building a client makes no network call here."""


def _captured(monkeypatch):
    calls = []
    monkeypatch.setattr(google_workspace, "credentials", lambda *a, **k: _Creds())
    monkeypatch.setattr(google_workspace, "_discovery_build", lambda *a, **k: calls.append((a, k)) or object())
    return calls


def test_every_pipeline_client_gets_a_timeout(monkeypatch):
    calls = _captured(monkeypatch)
    google_workspace.meet_client()
    google_workspace.drive_client()
    google_workspace.calendar_client()
    google_workspace.group_calendars_client()
    assert len(calls) == 4
    for _, kwargs in calls:
        assert "credentials" not in kwargs, "build() refuses credentials and http together"
        transport = kwargs["http"]
        assert transport.http.timeout == google_workspace.GOOGLE_HTTP_TIMEOUT_SECONDS


def test_the_timeout_is_finite_and_generous():
    assert 30 <= google_workspace.GOOGLE_HTTP_TIMEOUT_SECONDS <= 300
