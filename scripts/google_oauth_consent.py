#!/usr/bin/env python3
"""One-time OAuth consent for the recording pipeline's robot account.

Run this once, on a machine with a browser, and **sign in as
``recordings@mastereducation.kz``** when Google asks. It produces the refresh token that
``src/services/google_workspace.py`` uses to act as that account.

Why this exists instead of a service-account key: key creation is blocked on this GCP
account by ``iam.managed.disableServiceAccountKeyCreation`` and the policy cannot be
lifted. The refresh token is also the tighter credential — it can only ever act as the
account that consented, whereas a domain-wide-delegated key can impersonate anyone in
the domain. See spec §16.

Usage::

    export GOOGLE_OAUTH_CLIENT_ID=...
    export GOOGLE_OAUTH_CLIENT_SECRET=...
    python scripts/google_oauth_consent.py

The token is written to ``.google_refresh_token`` (mode 600) in the current directory
and is deliberately **not printed** — it is a credential, and printing it would put it
into terminal scrollback, shell history and any transcript of the session.
"""
import os
import stat
import sys

# Keep this list identical to google_workspace.SCOPES. A refresh token is bound to the
# scopes it was issued for: consenting to a narrower set than the code later requests
# fails at refresh time with `invalid_scope`, which is a confusing way to find out.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/meetings.space.created",
]

OUT_PATH = ".google_refresh_token"
EXPECTED_SUBJECT = "recordings@mastereducation.kz"


def main() -> int:
    client_id = (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        print("Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET first.",
              file=sys.stderr)
        return 2

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("pip install google-auth-oauthlib", file=sys.stderr)
        return 2

    print(f"A browser window will open. Sign in as {EXPECTED_SUBJECT} — NOT your own "
          f"account.\nRecordings land in whichever account consents here.\n")

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )
    # access_type=offline is what yields a refresh token at all; prompt=consent forces a
    # fresh one even if this account has already approved the app, which otherwise
    # returns an access token only and leaves you wondering where the refresh token went.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("No refresh token returned. Re-run; if it persists, revoke the app at "
              "https://myaccount.google.com/permissions and try again.", file=sys.stderr)
        return 1

    with open(OUT_PATH, "w") as fh:
        fh.write(creds.refresh_token)
    os.chmod(OUT_PATH, stat.S_IRUSR | stat.S_IWUSR)

    granted = set(creds.scopes or [])
    missing = [s for s in SCOPES if s not in granted]

    print(f"\nRefresh token written to {OUT_PATH} (mode 600). Not printed on purpose.")
    print(f"Length: {len(creds.refresh_token)} chars")
    if missing:
        print(f"\nWARNING: these scopes were not granted: {missing}", file=sys.stderr)
        print("The pipeline will fail on the corresponding calls.", file=sys.stderr)
        return 1
    print("All three scopes granted.")
    print("\nNext: hand the file to the deploy step, then delete it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
