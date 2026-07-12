"""Zitadel ("Master Education") account provisioning — SSO_DESIGN.md §E, implemented.

Until now Zitadel was ONLY a token issuer: nothing ever created accounts in it, so
"Continue with Master Education" was live on all four platforms but usable only by
hand-made console users. This module is the missing account factory:

  * ``provision_user()`` creates ONE Zitadel human user via the documented migration
    endpoint (``POST /management/v1/users/human/_import``), importing the user's existing
    LMS **bcrypt** hash so their current LMS password immediately works as their Master
    Education password (Zitadel verifies bcrypt out of the box and re-hashes on first
    login). Non-bcrypt (legacy Django pbkdf2) users are created without a password —
    email is imported as VERIFIED, so Zitadel's own reset flow works for them, and their
    per-platform passwords are untouched either way.
  * ``bulk_import()`` / the CLI walks the LMS ``users`` table (LMS is the identity
    system-of-record) and provisions everyone with an email — students AND staff — and
    stores the returned Zitadel user id into ``users.central_auth_user_id``, giving every
    platform a durable join key (the OIDC login already prefers/backfills it).

Idempotent: users already linked (central_auth_user_id set) are skipped; an
already-exists answer from Zitadel resolves the existing account by email and links it.

Auth: a Zitadel service user's Personal Access Token in ``ZITADEL_PAT`` (create in
console: Users -> Service Users -> new user with Org Owner/User Manager role -> Personal
Access Token). Empty PAT = module disabled; the CLI refuses to run.

Usage (inside the backend container / venv):

    python -m src.services.zitadel_provisioning --dry-run          # who WOULD be imported
    python -m src.services.zitadel_provisioning --import           # everyone with an email
    python -m src.services.zitadel_provisioning --import \
        --roles teacher,curator,head_curator,head_teacher,admin    # staff first
    python -m src.services.zitadel_provisioning --import --limit 50  # cautious batches
"""

from __future__ import annotations

import argparse
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def _base_url() -> str:
    return os.getenv("ZITADEL_BASE_URL", "https://mastereducation-fvbwjk.eu1.zitadel.cloud").rstrip("/")


def _pat() -> str:
    return os.getenv("ZITADEL_PAT", "").strip()


def zitadel_enabled() -> bool:
    return bool(_pat())


def _headers() -> dict:
    h = {"Authorization": f"Bearer {_pat()}", "Content-Type": "application/json"}
    org = os.getenv("ZITADEL_ORG_ID", "").strip()
    if org:
        h["x-zitadel-orgid"] = org
    return h


def _split_name(name: str) -> tuple[str, str]:
    """Zitadel requires non-empty first AND last name; degrade gracefully for one-word names."""
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return ("Master", "Student")
    if len(parts) == 1:
        return (parts[0], parts[0])
    return (parts[0], " ".join(parts[1:]))


class ZitadelError(RuntimeError):
    pass


def _find_user_id_by_email(client: httpx.Client, email: str) -> str | None:
    """Resolve an existing Zitadel user id by (unique) email via the management search API."""
    resp = client.post(
        f"{_base_url()}/management/v1/users/_search",
        headers=_headers(),
        json={"queries": [{"emailQuery": {"emailAddress": email, "method": "TEXT_QUERY_METHOD_EQUALS_IGNORE_CASE"}}]},
        timeout=15.0,
    )
    if resp.status_code != 200:
        return None
    for entry in (resp.json() or {}).get("result", []):
        uid = entry.get("id")
        if uid:
            return str(uid)
    return None


def provision_user(
    email: str,
    name: str,
    hashed_password: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> str:
    """Create (or resolve) the Zitadel account for one person; returns the Zitadel user id.

    Email is imported as VERIFIED (it is the cross-platform match key the OIDC logins
    enforce via email_verified). The bcrypt hash is imported only when it actually looks
    like bcrypt; anything else (legacy pbkdf2, empty) creates a password-less account.
    """
    if not zitadel_enabled():
        raise ZitadelError("ZITADEL_PAT is not configured")
    email = (email or "").strip()
    if not email or "@" not in email:
        raise ZitadelError(f"not an importable email: {email!r}")

    first, last = _split_name(name)
    body: dict = {
        "userName": email.lower(),
        "profile": {"firstName": first, "lastName": last, "displayName": (name or email).strip()},
        "email": {"email": email, "isEmailVerified": True},
    }
    if hashed_password and hashed_password[:4] in _BCRYPT_PREFIXES:
        body["hashedPassword"] = {"value": hashed_password, "algorithm": "bcrypt"}
        body["passwordChangeRequired"] = False

    own_client = client is None
    client = client or httpx.Client()
    try:
        resp = client.post(
            f"{_base_url()}/management/v1/users/human/_import",
            headers=_headers(),
            json=body,
            timeout=20.0,
        )
        if resp.status_code == 200:
            uid = (resp.json() or {}).get("userId")
            if not uid:
                raise ZitadelError(f"import succeeded but no userId in response: {resp.text[:200]}")
            return str(uid)
        # Already exists (Zitadel answers 409 or an AlreadyExists error body): link the existing one.
        if resp.status_code == 409 or "already" in resp.text.lower():
            existing = _find_user_id_by_email(client, email)
            if existing:
                return existing
        raise ZitadelError(f"import failed for {email}: HTTP {resp.status_code}: {resp.text[:300]}")
    finally:
        if own_client:
            client.close()


def mirror_password(zitadel_user_id: str | None, plaintext_password: str, *, lms_user_id=None) -> bool:
    """Best-effort: keep the user's Master Education (Zitadel) password equal to their LMS one.

    Called (as a FastAPI background task, off the response path) from the password-change/reset
    endpoints — the only places the plaintext legitimately exists in flight; it is never persisted
    or enqueued. Zitadel's SetPassword API accepts only plaintext (hash import exists only at user
    creation), hence an app-layer hook rather than a DB-trigger/outbox flow. Takes primitives, not
    the ORM object, so it is safe after the request's session has closed. STRICTLY non-fatal: any
    failure just means that user's ME password stays at its previous value (they can still reset
    it at the ME login page), and we log it.

    Returns True only when Zitadel actually accepted the new password.
    """
    try:
        if not zitadel_enabled():
            return False
        if not zitadel_user_id or not plaintext_password:
            return False
        resp = httpx.post(
            f"{_base_url()}/v2/users/{zitadel_user_id}/password",
            headers=_headers(),
            json={"newPassword": {"password": plaintext_password, "changeRequired": False}},
            timeout=5.0,
        )
        if resp.status_code == 200:
            return True
        logger.warning(
            "zitadel password mirror failed for user id=%s: HTTP %s: %s",
            lms_user_id, resp.status_code, resp.text[:200],
        )
        return False
    except Exception as exc:  # noqa: BLE001 - must never break a password change
        logger.warning("zitadel password mirror errored for user id=%s: %s", lms_user_id, exc)
        return False


def _get_user_state(client: httpx.Client, zitadel_user_id: str) -> str | None:
    """Return the Zitadel user's lifecycle state (e.g. 'USER_STATE_ACTIVE'/'USER_STATE_INACTIVE'),
    or None if it can't be read. Used to make (de)activation a no-op when already in the target
    state, rather than posting a redundant transition and depending on Zitadel's error wording."""
    try:
        r = client.get(
            f"{_base_url()}/management/v1/users/{zitadel_user_id}",
            headers=_headers(),
            timeout=10.0,
        )
        if r.status_code == 200:
            return ((r.json() or {}).get("user") or {}).get("state")
    except Exception as exc:  # noqa: BLE001
        logger.info("zitadel get_user_state(%s) unreadable: %s", zitadel_user_id, exc)
    return None


def set_user_active(zitadel_user_id: str, is_active: bool, *, client: httpx.Client | None = None) -> bool:
    """Deactivate/reactivate the Zitadel account to match LMS is_active. This is what makes an LMS
    deactivation a real, estate-wide revoke: a deactivated Zitadel user can no longer authenticate
    at the Master Education login at all (essential for the SSO-only future — without it a
    deactivated person could still log in via SSO).

    Idempotent by state pre-check: if the account is ALREADY in the target state we return success
    without posting a transition. This is the robust fix for the false-negative dead-letters where a
    routine identity edit (name/role) re-emitted a redundant _reactivate on an already-active user;
    Zitadel's precondition response was not always recognized as success, so a fully-delivered
    SAT+IELTS sync was wrongly marked failed and dead-lettered."""
    own_client = client is None
    client = client or httpx.Client()
    try:
        target_state = "USER_STATE_ACTIVE" if is_active else "USER_STATE_INACTIVE"
        if _get_user_state(client, zitadel_user_id) == target_state:
            return True  # already in the desired state — nothing to do (the common no-op case)
        action = "_reactivate" if is_active else "_deactivate"
        r = client.post(
            f"{_base_url()}/management/v1/users/{zitadel_user_id}/{action}",
            headers=_headers(),
            json={},
            timeout=10.0,
        )
        if r.status_code == 200:
            return True
        # Belt-and-suspenders for a race between the state read and the post: Zitadel answers a
        # redundant transition with a precondition/AlreadyExists-style error — treat it as success.
        if r.status_code in (409, 400) and ("already" in r.text.lower() or "state" in r.text.lower()):
            return True
        logger.warning("zitadel set_user_active(%s,%s) failed: HTTP %s: %s", zitadel_user_id, is_active, r.status_code, r.text[:200])
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("zitadel set_user_active(%s) errored: %s", zitadel_user_id, exc)
        return False
    finally:
        if own_client:
            client.close()


def _is_noop_update(r) -> bool:
    """True when a Zitadel update PUT either succeeded (200) OR was a no-op it rejects with a
    precondition error because the value already matches — e.g. profile 'Profile not changed'
    (COMMAND-2M0fs) or an unchanged email/username. A no-op means the desired state is already
    present, so the caller must treat it as success; failing it dead-letters an event whose SAT/
    IELTS side already delivered (the root cause of the false-negative backlog)."""
    if r.status_code == 200:
        return True
    if r.status_code in (400, 409):
        t = r.text.lower()
        return "not changed" in t or "already" in t
    return False


def update_user(
    zitadel_user_id: str,
    *,
    email: str | None = None,
    name: str | None = None,
    old_email: str | None = None,
    is_active: bool | None = None,
    client: httpx.Client | None = None,
) -> bool:
    """Update a linked Zitadel account's email/username/name/active-state to match LMS (called from
    the user.upserted drainer). Without the email part, an email change leaves Zitadel on the OLD
    address while the platforms move to the NEW one — and SSO (which matches by verified email)
    404s. Without the active part, an LMS deactivation never disables SSO.

    Email is re-imported as VERIFIED and the username is moved with it (both are login handles).
    Returns True when every attempted update succeeded (or nothing needed changing)."""
    own_client = client is None
    client = client or httpx.Client()
    ok = True
    try:
        base, headers = _base_url(), _headers()
        email_changed = bool(
            email and old_email and email.strip().lower() != old_email.strip().lower()
        )
        if email_changed:
            e = email.strip()
            r = client.put(
                f"{base}/management/v1/users/{zitadel_user_id}/email",
                headers=headers,
                json={"email": e, "isEmailVerified": True},
                timeout=10.0,
            )
            if not _is_noop_update(r):
                ok = False
                logger.warning("zitadel email update %s failed: HTTP %s: %s", zitadel_user_id, r.status_code, r.text[:200])
            # Keep the login name in step with the email; a conflict here is non-fatal (email is
            # already a valid login handle once verified).
            try:
                client.put(
                    f"{base}/management/v1/users/{zitadel_user_id}/username",
                    headers=headers,
                    json={"userName": e.lower()},
                    timeout=10.0,
                )
            except httpx.HTTPError:
                pass
        if name and name.strip():
            first, last = _split_name(name)
            r = client.put(
                f"{base}/management/v1/users/{zitadel_user_id}/profile",
                headers=headers,
                json={"firstName": first, "lastName": last, "displayName": name.strip()},
                timeout=10.0,
            )
            if not _is_noop_update(r):
                ok = False
                logger.warning("zitadel profile update %s failed: HTTP %s: %s", zitadel_user_id, r.status_code, r.text[:200])
        if is_active is not None:
            ok = set_user_active(zitadel_user_id, is_active, client=client) and ok
        return ok
    except Exception as exc:  # noqa: BLE001 - never crash the drain loop
        logger.warning("zitadel update_user %s errored: %s", zitadel_user_id, exc)
        return False
    finally:
        if own_client:
            client.close()


def bulk_import(db, *, roles: list[str] | None = None, limit: int | None = None,
                dry_run: bool = False, sleep_seconds: float = 0.15) -> dict:
    """Provision every active, email-bearing, not-yet-linked LMS user into Zitadel.

    Writes the Zitadel user id into users.central_auth_user_id (the durable join key),
    committing per user so a mid-run failure loses nothing. Returns counters.
    """
    from src.auth.models import UserInDB

    q = (
        db.query(UserInDB)
        .filter(UserInDB.is_active.is_(True))
        .filter(UserInDB.email.isnot(None))
        .filter((UserInDB.central_auth_user_id.is_(None)) | (UserInDB.central_auth_user_id == ""))
        .order_by(UserInDB.id)
    )
    if roles:
        q = q.filter(UserInDB.role.in_(roles))
    if limit:
        q = q.limit(limit)
    users = q.all()

    counters = {"candidates": len(users), "imported": 0, "linked_existing": 0,
                "with_password": 0, "without_password": 0, "errors": 0}
    if dry_run:
        for u in users[:15]:
            has_pw = bool(u.hashed_password and u.hashed_password[:4] in _BCRYPT_PREFIXES)
            print(f"  would import: id={u.id} role={u.role} pw={'bcrypt' if has_pw else 'none'}")
        if len(users) > 15:
            print(f"  ... and {len(users) - 15} more")
        return counters

    with httpx.Client() as client:
        for u in users:
            try:
                has_pw = bool(u.hashed_password and u.hashed_password[:4] in _BCRYPT_PREFIXES)
                uid = provision_user(u.email, u.name, u.hashed_password, client=client)
                u.central_auth_user_id = uid
                db.commit()
                counters["imported"] += 1
                counters["with_password" if has_pw else "without_password"] += 1
                if counters["imported"] % 50 == 0:
                    logger.info("zitadel bulk import: %s/%s done", counters["imported"], len(users))
                time.sleep(sleep_seconds)  # be a polite API citizen (Zitadel Cloud rate limits)
            except ZitadelError as exc:
                db.rollback()
                counters["errors"] += 1
                logger.warning("zitadel import failed for user id=%s: %s", u.id, exc)
    return counters


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision LMS users into Zitadel (Master Education)")
    parser.add_argument("--import", dest="do_import", action="store_true", help="actually import")
    parser.add_argument("--dry-run", action="store_true", help="list who would be imported")
    parser.add_argument("--roles", default=None, help="comma-separated LMS roles filter")
    parser.add_argument("--limit", type=int, default=None, help="max users this run")
    args = parser.parse_args()

    if not zitadel_enabled():
        raise SystemExit("ZITADEL_PAT is not set — create a service-user PAT in the Zitadel console first.")
    if not (args.do_import or args.dry_run):
        raise SystemExit("pass --dry-run to preview or --import to run")

    from src.config import SessionLocal

    roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else None
    db = SessionLocal()
    try:
        result = bulk_import(db, roles=roles, limit=args.limit, dry_run=not args.do_import)
        print("result:", result)
    finally:
        db.close()


if __name__ == "__main__":
    main()
