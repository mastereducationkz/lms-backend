"""Provision the missing teacher/curator (staff) accounts on IELTS and SAT/NUET.

Background
----------
The group-sync drainer HTTP-pushes ``group.upserted`` events (now carrying the group's
teacher/curator identity) to the target platforms. On the target, a group can only attach its
teacher/curator (``teacher_id`` / ``curator_id``) if that staff member already has a
*platform-local* account, matched by email. Many teachers/curators run LMS groups but were never
provisioned on the group's target platform, so the group consumer cannot resolve the staff FK and
leaves the group unlinked — and the staff member cannot SSO-login there either.

These staff already have a Zitadel account (they are LMS users), so SSO would work — what is
missing is the platform-local record the group link needs. Both platforms expose an
**idempotent-on-email** staff provisioning endpoint (an existing account is returned untouched):

  * IELTS: ``POST {IELTS_SYNC_URL}/staff``. The IELTS router is mounted under ``/lms`` and
    ``IELTS_SYNC_URL`` already carries that ``/lms`` suffix. Auth: ``X-API-Key`` = ``IELTS_API_KEY``.
  * SAT/NUET: ``POST {SAT_SYNC_URL}/staff``. ``SAT_SYNC_URL`` defaults to
    ``https://api.mastereducation.kz/api/lms``. Auth: ``X-API-Key`` = ``MASTEREDU_API_KEY``.

Both endpoints take the same envelope as the other ``/api/lms`` sync endpoints::

    { "event_id": "<uuid>", "event_type": "staff.upserted", "source": "lms_provision_staff",
      "staff": { "lms_user_id": 123, "email": "t@x.io", "name": "Ms T", "role": "teacher" } }

``role`` here is the *platform* role family ("teacher" | "curator"). The endpoint is idempotent on
the normalized (trim+lower) email: an existing user/staff row is returned untouched (``created``
false), otherwise a new teacher/curator with a random placeholder password (SSO-only) is created.

This tool does NOT link groups
------------------------------
This CLI only creates the missing platform-local staff accounts. After running it, re-run the
GROUP backfill so the group consumers resolve the now-existing staff and set the FK::

    python -m src.services.sync_backfill --groups

Usage (inside the backend container / venv, same env as the app)::

    python -m src.services.sync_provision_staff                     # provision all missing staff
    python -m src.services.sync_provision_staff --platform ielts    # IELTS only
    python -m src.services.sync_provision_staff --platform sat       # SAT/NUET only
    python -m src.services.sync_provision_staff --dry-run            # list who WOULD be provisioned
    python -m src.services.sync_provision_staff --limit 20           # cap the number provisioned
"""

from __future__ import annotations

import argparse
import logging
import os
import uuid

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

# Reuse the internal/test-email filter so we never provision an @lms.com / test account onto a
# real platform. (Imported, not redefined, so the two tools agree on what "internal" means.)
from src.services.sync_provision_gaps import _is_test_email

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


def _models():
    # Lazy import to avoid an import cycle (courses.models <-> services at load time).
    from src.auth.models import UserInDB
    from src.courses.models import Group, GroupStudent

    return Group, GroupStudent, UserInDB


# --- role mapping ------------------------------------------------------------
# LMS role -> platform role FAMILY. Only teacher/curator families are provisioned; every other
# LMS role (student, admin, parent, head_curator-into-curator etc.) maps 1:1 by family below.
_TEACHER_FAMILY = {"teacher", "head_teacher"}
_CURATOR_FAMILY = {"curator", "head_curator"}


def _platform_role(lms_role: str) -> str | None:
    """Map an LMS role to the platform role family, or None if it isn't staff-provisionable."""
    role = (lms_role or "").strip().lower()
    if role in _TEACHER_FAMILY:
        return "teacher"
    if role in _CURATOR_FAMILY:
        return "curator"
    return None


# --- staff discovery ---------------------------------------------------------
# A staff member is provisioned onto a platform if they run an LMS group WITH MEMBERS on that
# platform's program. program_type ielts -> IELTS; program_type in (sat, nuet) -> SAT. A group
# references its teacher via teacher_id (teacher family) and its curator via curator_id (curator
# family), so the SAME user can be provisioned as "teacher" on one platform and "curator" on
# another depending on which slot references them in which group.


class ProvisionStaff:
    """A distinct staff member to provision on ONE target platform, with ONE platform role."""

    __slots__ = ("lms_user_id", "email", "name", "role", "platform")

    def __init__(self, lms_user_id: int, email: str, name: str | None, role: str, platform: str):
        self.lms_user_id = lms_user_id
        self.email = email
        self.name = name
        self.role = role  # platform role family: "teacher" | "curator"
        self.platform = platform  # "ielts" | "sat"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<ProvisionStaff {self.email} -> {self.platform} as {self.role}>"


def _platform_of(program_type: str) -> str | None:
    """LMS group program_type -> target platform, or None for programs with no staff target."""
    program = (program_type or "").strip().lower()
    if program == "ielts":
        return "ielts"
    if program in ("sat", "nuet"):
        return "sat"
    return None


def find_staff_to_provision(db: Session) -> list[ProvisionStaff]:
    """Distinct staff (teacher_id/curator_id) of groups that HAVE members, grouped by platform+role.

    Returns one ProvisionStaff per (email, platform, role). A user who is a teacher on a SAT group
    and a curator on an IELTS group yields two entries (different platform and role). Null / test /
    non-staff-role emails are skipped.
    """
    Group, GroupStudent, UserInDB = _models()

    # Only groups that actually have members — an empty group has nothing to link and no reason to
    # drag its staff onto a platform. EXISTS keeps it a single indexed subquery.
    has_members = db.query(GroupStudent.id).filter(GroupStudent.group_id == Group.id).exists()
    groups = (
        db.query(Group.teacher_id, Group.curator_id, Group.program_type)
        .filter(has_members)
        .filter(or_(Group.teacher_id.isnot(None), Group.curator_id.isnot(None)))
        .all()
    )

    # (email, platform, role) -> ProvisionStaff, deduped. Collect the (user_id, slot) pairs per
    # platform first, then resolve identities in one query.
    wanted: dict[str, set[tuple[int, str]]] = {}  # platform -> {(user_id, slot)}
    for teacher_id, curator_id, program_type in groups:
        platform = _platform_of(program_type)
        if platform is None:
            continue  # program with no staff target (e.g. general_english)
        bucket = wanted.setdefault(platform, set())
        if teacher_id is not None:
            bucket.add((teacher_id, "teacher"))  # teacher_id slot -> teacher family
        if curator_id is not None:
            bucket.add((curator_id, "curator"))  # curator_id slot -> curator family

    all_ids = {uid for pairs in wanted.values() for uid, _ in pairs}
    if not all_ids:
        return []
    users = (
        db.query(UserInDB.id, UserInDB.email, UserInDB.name, UserInDB.role)
        .filter(UserInDB.id.in_(all_ids))
        .all()
    )
    by_id = {u.id: u for u in users}

    seen: set[tuple[str, str, str]] = set()  # (email, platform, role)
    out: list[ProvisionStaff] = []
    for platform in sorted(wanted):
        for uid, slot in sorted(wanted[platform]):
            user = by_id.get(uid)
            if user is None:
                continue
            email = (user.email or "").strip().lower()
            if not email:
                continue  # nothing to match on; the staff endpoint keys on email
            if _is_test_email(email):
                continue  # never provision LMS internal/test accounts onto a real platform
            # The platform role is the group SLOT's family (teacher_id -> teacher, curator_id ->
            # curator). We double-check the user's own LMS role is a staff role; a student/admin
            # accidentally sitting in a teacher_id slot must not be provisioned as staff.
            role_from_user = _platform_role(user.role)
            if role_from_user is None:
                continue  # user's LMS role is not teacher/curator family — skip
            role = slot  # the slot decides teacher vs curator for THIS group's link
            key = (email, platform, role)
            if key in seen:
                continue
            seen.add(key)
            name = user.name.strip() if isinstance(user.name, str) and user.name.strip() else None
            out.append(ProvisionStaff(uid, email, name, role, platform))
    return out


# --- provisioning targets ----------------------------------------------------


def _ielts_base() -> str:
    return os.getenv("IELTS_SYNC_URL", "").rstrip("/")


def _sat_base() -> str:
    return os.getenv("SAT_SYNC_URL", "https://api.mastereducation.kz/api/lms").rstrip("/")


def _provision_one(staff: ProvisionStaff, *, timeout: float = _TIMEOUT) -> tuple[str, str]:
    """POST one staff member to its target ``/staff`` endpoint.

    Returns (outcome, detail) where outcome is one of:
      "created" — a new platform-local staff account was created (HTTP 201, created=true)
      "exists"  — the account already existed, returned untouched (HTTP 200, created=false)
      "error"   — misconfiguration / transport / non-2xx (detail says why)
    Idempotent: re-running is safe (an existing account is returned untouched, never modified).
    """
    if staff.platform == "ielts":
        base = _ielts_base()
        key = os.getenv("IELTS_API_KEY", "")
    else:  # "sat"
        base = _sat_base()
        key = os.getenv("MASTEREDU_API_KEY", "")

    if not base:
        return "error", f"{staff.platform}: base URL not configured"
    if not key:
        return "error", f"{staff.platform}: api key not configured"

    url = f"{base}/staff"
    body = {
        "event_id": str(uuid.uuid4()),
        "event_type": "staff.upserted",
        "source": "lms_provision_staff",
        "staff": {
            "lms_user_id": staff.lms_user_id,
            "email": staff.email,
            "name": staff.name,
            "role": staff.role,
        },
    }

    try:
        resp = httpx.post(
            url,
            json=body,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return "error", f"{staff.platform}: transport error: {exc}"

    if resp.status_code in (200, 201):
        created = _created_flag(resp)
        if created is True or resp.status_code == 201:
            return "created", f"{staff.platform}: created {staff.email} as {staff.role}"
        return "exists", f"{staff.platform}: exists {staff.email}"
    return "error", f"{staff.platform}: HTTP {resp.status_code}: {resp.text[:160]}"


def _created_flag(resp: httpx.Response):
    """Read the body's `created` bool; None if the body isn't the expected JSON."""
    try:
        data = resp.json()
    except Exception:
        return None
    if isinstance(data, dict) and "created" in data:
        return bool(data["created"])
    return None


# --- inline provisioning (shared with the drainer) ---------------------------
# The batch CLI above discovers WHO to provision from the group graph. The drainer needs the same
# wire contract but for ONE known user/group event at a time (no discovery, no pacing), so these
# helpers factor out "build the envelope + POST it" without pulling in the CLI's group scan. Keeping
# them here means the drainer and the CLI can never drift on the staff.upserted shape or role map.

_ALL_PLATFORMS = ("ielts", "sat")


def _platform_configured(platform: str) -> bool:
    """True when a platform has BOTH a base URL and an API key set — i.e. worth POSTing to.

    Filtering here (rather than letting ``_provision_one`` return a "not configured" error) keeps
    the drainer's outcome clean: a genuinely-configured platform that then errors is transient
    (retry), never a config gap masquerading as a failure.
    """
    if platform == "ielts":
        return bool(_ielts_base()) and bool(os.getenv("IELTS_API_KEY", ""))
    if platform == "sat":
        return bool(_sat_base()) and bool(os.getenv("MASTEREDU_API_KEY", ""))
    return False


def staff_entry(
    lms_user_id: int, email: str | None, name: str | None, platform_role: str, platform: str
) -> ProvisionStaff | None:
    """Build ONE ProvisionStaff for a known (role, platform), or None if it must be skipped.

    Skips empty/internal/test emails (never provision an @lms.com/test account onto a real
    platform) and unconfigured/unknown platforms. ``platform_role`` is the platform family
    ("teacher"|"curator") — for a group event it is the SLOT (teacher_email->teacher).
    """
    if platform_role not in ("teacher", "curator"):
        return None
    if not _platform_configured(platform):
        return None
    email_norm = (email or "").strip().lower()
    if not email_norm or _is_test_email(email_norm):
        return None
    nm = name.strip() if isinstance(name, str) and name.strip() else None
    return ProvisionStaff(lms_user_id, email_norm, nm, platform_role, platform)


def staff_entries_for_user(
    lms_user_id: int,
    email: str | None,
    name: str | None,
    lms_role: str | None,
    platforms: tuple[str, ...] = _ALL_PLATFORMS,
) -> list[ProvisionStaff]:
    """Entries to make a newly-created LMS staff user exist on every configured platform.

    Uses the user's OWN LMS role to pick the platform family (teacher vs curator); returns [] for
    non-staff roles (students/admins/parents) so the caller is a no-op for them.
    """
    platform_role = _platform_role(lms_role or "")
    if platform_role is None:
        return []
    out = []
    for platform in platforms:
        entry = staff_entry(lms_user_id, email, name, platform_role, platform)
        if entry is not None:
            out.append(entry)
    return out


def provision_staff_now(
    entries: list[ProvisionStaff], *, timeout: float = _TIMEOUT
) -> list[tuple[ProvisionStaff, str, str]]:
    """POST each entry to its ``/staff`` endpoint immediately (no pacing). For the drainer, which
    handles a handful of staff per event and wants low latency. Returns (staff, outcome, detail)
    with outcome in created|exists|error (same as ``_provision_one``). Idempotent per entry."""
    results = []
    for s in entries:
        outcome, detail = _provision_one(s, timeout=timeout)
        results.append((s, outcome, detail))
    return results


# --- driver ------------------------------------------------------------------


def provision_staff(
    db: Session,
    *,
    platform: str = "all",
    dry_run: bool = False,
    limit: int | None = None,
    sleep_seconds: float = 0.6,
) -> dict:
    """Provision the missing platform-local staff accounts for teachers/curators of active groups.

    ``platform`` is "ielts", "sat" or "all". ``dry_run`` lists who would be provisioned and makes
    NO HTTP calls. ``limit`` caps how many staff are processed (after filtering/ordering).
    Returns a result dict with per-platform and total tallies.
    """
    platform = (platform or "all").strip().lower()
    if platform not in ("ielts", "sat", "all"):
        raise ValueError(f"platform must be ielts|sat|all, got {platform!r}")

    staff = find_staff_to_provision(db)
    if platform != "all":
        staff = [s for s in staff if s.platform == platform]
    if limit is not None:
        staff = staff[:limit]

    result: dict = {
        "platform": platform,
        "dry_run": dry_run,
        "total": len(staff),
        "created": 0,
        "exists": 0,
        "error": 0,
        "by_platform": {
            "ielts": {"total": 0, "created": 0, "exists": 0, "error": 0},
            "sat": {"total": 0, "created": 0, "exists": 0, "error": 0},
        },
        "errors": [],
    }

    for s in staff:
        result["by_platform"][s.platform]["total"] += 1

    if dry_run:
        result["would_provision"] = [
            {
                "lms_user_id": s.lms_user_id,
                "email": s.email,
                "name": s.name,
                "role": s.role,
                "platform": s.platform,
            }
            for s in staff
        ]
        return result

    # Pace requests to stay comfortably under the platforms' rate limits (~100/min at 0.6s).
    import time as _time

    for i, s in enumerate(staff):
        if i and sleep_seconds:
            _time.sleep(sleep_seconds)
        outcome, detail = _provision_one(s)
        result[outcome] += 1
        result["by_platform"][s.platform][outcome] += 1
        if outcome == "error":
            result["errors"].append(detail)
        else:
            logger.info("provision-staff: %s", detail)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision platform-local teacher/curator accounts so synced groups can link them"
    )
    parser.add_argument(
        "--platform",
        choices=["ielts", "sat", "all"],
        default="all",
        help="which target platform to provision (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list who would be provisioned; make NO provisioning calls",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the number of staff processed",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.6,
        help="seconds between provisioning requests, to stay under platform rate limits (default 0.6)",
    )
    args = parser.parse_args()

    from src.config import SessionLocal

    db = SessionLocal()
    try:
        result = provision_staff(
            db,
            platform=args.platform,
            dry_run=args.dry_run,
            limit=args.limit,
            sleep_seconds=args.sleep,
        )
        print("provision-staff:", result)
        print(
            "\nnext step (not run by this tool): re-run the group backfill so the group consumers "
            "resolve the now-existing staff and set teacher_id/curator_id:\n"
            "  python -m src.services.sync_backfill --groups"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
