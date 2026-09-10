"""Credentials and scope handling for the Google Workspace recording pipeline.

The pipeline authenticates as one account — the ``recordings@`` robot — with an OAuth
refresh token, because service-account keys cannot be created on this GCP account
(``iam.managed.disableServiceAccountKeyCreation`` is enforced and unliftable; spec §16).

Two things are worth pinning with tests. First, the scope list: a refresh token is bound
to the scopes it was issued for, so silently widening ``SCOPES`` later produces an
``invalid_scope`` failure at refresh time in production rather than anything legible.
Second, the enable predicate: this pipeline reaches into a real Drive and a real
Calendar, so "configured" and "switched on" must stay two separate conditions, the same
way ``ENABLE_VIDEO_INGEST`` gates video ingest.

No network: every test either inspects the constructed credentials object or captures
what would have been handed to ``build()``.
"""
import pytest

from src.services import google_workspace


ENV_VARS = (
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REFRESH_TOKEN",
    "ENABLE_RECORDINGS",
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def configured_env(clean_env):
    clean_env.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    clean_env.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "csecret")
    clean_env.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "rtoken")
    return clean_env


# --- scopes ------------------------------------------------------------------

def test_scopes_are_exactly_what_was_consented():
    """Guard against silent widening: the refresh token is bound to this set."""
    assert google_workspace.SCOPES == [
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/meetings.space.created",
    ]


def test_readonly_meet_scope_is_not_requested():
    """`meetings.space.readonly` would expose every meeting in the org, not just ours."""
    assert "https://www.googleapis.com/auth/meetings.space.readonly" not in google_workspace.SCOPES


# --- credentials -------------------------------------------------------------

def test_credentials_built_from_env(configured_env):
    creds = google_workspace.credentials()

    assert creds.refresh_token == "rtoken"
    assert creds.client_id == "cid.apps.googleusercontent.com"
    assert creds.client_secret == "csecret"
    assert creds.token is None, "no access token should be baked in; it is fetched on use"
    assert list(creds.scopes) == google_workspace.SCOPES


@pytest.mark.parametrize("missing", [
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REFRESH_TOKEN",
])
def test_credentials_name_what_is_missing(configured_env, missing):
    """A deployment with no Google config must fail legibly, not as an auth error."""
    configured_env.delenv(missing)

    with pytest.raises(google_workspace.GoogleWorkspaceNotConfigured) as excinfo:
        google_workspace.credentials()
    assert missing in str(excinfo.value)


def test_blank_env_counts_as_missing(configured_env):
    """`FOO=` in a .env file is absence, not an empty-string credential."""
    configured_env.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "   ")

    with pytest.raises(google_workspace.GoogleWorkspaceNotConfigured):
        google_workspace.credentials()


# --- enable predicate --------------------------------------------------------

def test_not_enabled_without_the_flag(configured_env):
    """Credentials present but the switch off is the normal, safe state."""
    assert google_workspace.recordings_enabled() is False


def test_not_enabled_without_credentials(clean_env):
    clean_env.setenv("ENABLE_RECORDINGS", "true")
    assert google_workspace.recordings_enabled() is False


def test_enabled_needs_both(configured_env):
    configured_env.setenv("ENABLE_RECORDINGS", "true")
    assert google_workspace.recordings_enabled() is True


# --- clients -----------------------------------------------------------------

@pytest.fixture
def captured_build(monkeypatch):
    calls = []

    def _fake_build(api, version, credentials=None, cache_discovery=None):
        calls.append({"api": api, "version": version,
                      "credentials": credentials, "cache_discovery": cache_discovery})
        return object()

    monkeypatch.setattr(google_workspace, "_discovery_build", _fake_build)
    return calls


@pytest.mark.parametrize("factory,api,version", [
    ("calendar_client", "calendar", "v3"),
    ("drive_client", "drive", "v3"),
    ("meet_client", "meet", "v2"),
])
def test_clients_target_the_right_api(configured_env, captured_build, factory, api, version):
    getattr(google_workspace, factory)()

    assert captured_build[0]["api"] == api
    assert captured_build[0]["version"] == version
    assert captured_build[0]["credentials"].refresh_token == "rtoken"
    # The container's discovery cache is unwritable; leaving this on logs a warning per call.
    assert captured_build[0]["cache_discovery"] is False


def test_client_refuses_when_unconfigured(clean_env, captured_build):
    with pytest.raises(google_workspace.GoogleWorkspaceNotConfigured):
        google_workspace.drive_client()
    assert captured_build == [], "must not reach build() without credentials"
