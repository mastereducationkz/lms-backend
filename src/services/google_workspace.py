"""Google Workspace API access for the lesson/sales recording pipeline.

The single place where credentials and scopes are decided. Every other module in the
pipeline asks this one for a client and never builds credentials itself.

**Why an OAuth refresh token and not a service account.** The obvious design — a service
account with domain-wide delegation impersonating ``recordings@`` — is not available:
``iam.managed.disableServiceAccountKeyCreation`` is enforced on this GCP account as part
of Google's secure-by-default baseline, its policy source is "inherit parent's policy",
and there is no organization to hold ``roles/orgpolicy.policyAdmin``. Downloadable
service-account keys simply cannot be created.

That turned out to be the safer design anyway. A refresh token can only ever act as the
one account that consented — ``recordings@mastereducation.kz``. A domain-wide-delegated
key can impersonate *every* user in the domain, with full Drive access, and Google
provides no way to narrow a delegation to a single subject. See spec §16.

Credentials come from the environment and are never committed:

* ``GOOGLE_OAUTH_CLIENT_ID`` / ``GOOGLE_OAUTH_CLIENT_SECRET`` — the Desktop-app OAuth
  client from the ``master-education-workspace`` project.
* ``GOOGLE_OAUTH_REFRESH_TOKEN`` — obtained once via ``scripts/google_oauth_consent.py``,
  signed in as the robot.
* ``ENABLE_RECORDINGS`` — kill switch, defaults off, mirroring ``ENABLE_VIDEO_INGEST``.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"

# Exactly the scopes granted at consent. Kept as one list because a refresh token is
# bound to the scope set it was issued for — widening this without re-consenting yields
# an invalid_scope error at refresh time rather than a useful failure.
#
# `drive` (not `drive.file`) is required: the recording is created by Meet, not by this
# app, so `drive.file` can never see it, and the pipeline has to copy it into the Shared
# Drive and delete the original at 7 days (spec §4.6).
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/meetings.space.created",
]

# Shared Drive ids come from the environment so a staging deployment can point at
# different drives. The defaults are the production drives, so a missing env var
# degrades to "correct in prod" rather than to an empty id that fails obscurely.
RECORDINGS_SHARED_DRIVE_ID = os.getenv("RECORDINGS_SHARED_DRIVE_ID") or "0AAR1I-vUrpFRUk9PVA"
SALES_SHARED_DRIVE_ID = os.getenv("SALES_SHARED_DRIVE_ID") or "0ABz89lc-3YQ5Uk9PVA"


class GoogleWorkspaceNotConfigured(RuntimeError):
    """Raised when the OAuth environment is incomplete.

    Its own type so callers can tell "this deployment has no Google credentials" apart
    from "Google rejected our credentials", which is a very different incident.
    """


def _env(name: str) -> Optional[str]:
    value = os.getenv(name)
    return value.strip() if value else None


def recordings_enabled() -> bool:
    """True only when the pipeline is both configured and switched on.

    Two conditions, not one: a deployment can carry credentials while the feature stays
    off (the default), exactly as ``ENABLE_VIDEO_INGEST`` works.
    """
    return bool(
        _env("ENABLE_RECORDINGS") == "true"
        and _env("GOOGLE_OAUTH_CLIENT_ID")
        and _env("GOOGLE_OAUTH_CLIENT_SECRET")
        and _env("GOOGLE_OAUTH_REFRESH_TOKEN")
    )


def credentials(scopes: Optional[list] = None):
    """Build credentials for the robot account from the refresh token.

    No access token is passed: google-auth fetches one on first use and refreshes it
    thereafter, so nothing long-lived beyond the refresh token is ever held in memory.

    ``Credentials`` is imported here rather than at module scope so that a module whose
    main job is answering ``recordings_enabled()`` does not drag the google-auth tree in
    at import time.

    (An earlier version of this comment blamed ``pyu2f`` USB-key enumeration for import
    hangs on the dev Mac. That was wrong: ``faulthandler`` showed the process parked in
    ``importlib._bootstrap_external.get_data`` — file I/O — because the venv lives under
    ``~/Documents`` and iCloud had evicted it. Same import: >120 s from that venv, 1.06 s
    from ``/tmp``. Nothing to do with this code.)
    """
    from google.oauth2.credentials import Credentials

    client_id = _env("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = _env("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = _env("GOOGLE_OAUTH_REFRESH_TOKEN")
    missing = [
        name
        for name, value in (
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_OAUTH_REFRESH_TOKEN", refresh_token),
        )
        if not value
    ]
    if missing:
        raise GoogleWorkspaceNotConfigured(f"missing env: {', '.join(missing)}")

    return Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=list(scopes or SCOPES),
    )


def _discovery_build(*args, **kwargs):
    """Import googleapiclient lazily.

    ``googleapiclient.discovery`` pulls in a large dependency tree, and this module is
    imported by code (and tests) that only ever wants ``recordings_enabled()``. Keeping
    the import inside the call means the cost is paid once, by the worker that actually
    talks to Google, and never at process start.
    """
    from googleapiclient.discovery import build

    return build(*args, **kwargs)


def _client(api: str, version: str):
    # cache_discovery=False: the default file cache is unwritable in the container and
    # logs a warning on every single build() call.
    return _discovery_build(api, version, credentials=credentials(), cache_discovery=False)


def calendar_client():
    return _client("calendar", "v3")


def drive_client():
    return _client("drive", "v3")


def meet_client():
    return _client("meet", "v2")
