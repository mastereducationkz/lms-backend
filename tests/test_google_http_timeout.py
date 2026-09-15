"""Every Google API client is built on an HTTP transport with a deadline, sharing one credentials.

2026-09-15: attendance landed more than an hour after the lessons. Each client opened with its own
token exchange (1–9 s), and the recordings worker builds a client per Meet call, so one tick took
over half an hour. The transport's deadline is ours to set because we pass ``http=``.
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


def test_clients_share_one_credentials_so_the_token_is_fetched_once(monkeypatch):
    for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"):
        monkeypatch.setenv(name, "configured")
    monkeypatch.setattr(google_workspace, "_SHARED_CREDENTIALS", {})
    made = []
    monkeypatch.setattr(google_workspace, "credentials", lambda *a, **k: made.append(a) or _Creds())
    transports = []
    monkeypatch.setattr(google_workspace, "_discovery_build", lambda *a, **k: transports.append(k["http"]) or object())

    for _ in range(3):
        google_workspace.meet_client()
    google_workspace.drive_client()

    assert len(made) == 1, "one token for every client, not a token exchange per client"
    assert len({id(t.credentials) for t in transports}) == 1
    assert len({id(t) for t in transports}) == 4, "httplib2 is not thread-safe: each client keeps its own transport"


def test_unconfigured_credentials_are_never_shared(monkeypatch):
    for name in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(google_workspace, "_SHARED_CREDENTIALS", {})
    monkeypatch.setattr(google_workspace, "_discovery_build", lambda *a, **k: object())

    for _ in range(2):
        try:
            google_workspace.meet_client()
        except google_workspace.GoogleWorkspaceNotConfigured:
            pass
        else:
            raise AssertionError("an unconfigured client must refuse, every time")
    assert google_workspace._SHARED_CREDENTIALS == {}
