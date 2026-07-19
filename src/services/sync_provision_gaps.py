"""Provision the gap students — the ones stuck in dead-lettered membership sync.

Background
----------
The membership drainer (``student_sync``) HTTP-pushes ``member.upserted`` events to the target
platforms (IELTS, SAT/NUET). A student's membership can only attach on the target if that student
already has a *platform-local* account, matched by email. Roughly ~218 students sit in LMS groups
but have never been provisioned on their target platform, so their ``member.upserted`` rows kept
404'ing ("student not found") and, after the retry budget ran out, dead-lettered
(``status='failed'`` in ``student_sync_outbox`` with ``last_error`` like *not found* / *404*).

These students already have a Zitadel account (they are LMS users), so SSO would work — what is
missing is the platform-local record the membership call needs. Both platforms expose an
**idempotent-on-email** provisioning endpoint (an existing account is returned untouched):

  * IELTS: ``POST {IELTS_SYNC_URL}/students/`` (the CRM «Сделать клиентом» route). The IELTS
    router is mounted under ``/lms`` and ``IELTS_SYNC_URL`` already carries that ``/lms`` suffix
    (the same base the membership drainer posts ``/students/membership`` to). Auth: ``X-API-Key``
    header = ``IELTS_API_KEY``.
  * SAT/NUET: ``POST {SAT_SYNC_URL}/students`` with an extra ``product`` field
    (``"SAT" | "NUET" | "BOTH"``). ``SAT_SYNC_URL`` defaults to
    ``https://api.mastereducation.kz/api/lms``. Auth: ``X-API-Key`` header = ``MASTEREDU_API_KEY``.

Credentials email is suppressed
-------------------------------
Both provisioning endpoints send a branded credentials email on *creation* by default
(``send_credentials`` defaults to ``true`` on both). Because these students authenticate via SSO
("Continue with Master Education"), a per-student credentials email is unwanted — so this tool
**always sends ``send_credentials=false``**. (The SAT endpoint only emails when the request flag
is set; the IELTS endpoint likewise only emails when ``send_credentials`` is truthy.) An existing
account is returned with ``created=false`` and is never emailed regardless.

This tool does NOT replay
-------------------------
This CLI only creates the missing platform-local accounts. It deliberately does **not** touch the
outbox. After provisioning, the operator re-runs the replay CLI to re-queue the dead-lettered
membership rows so the drainer attaches the memberships::

    python -m src.services.sync_replay --replay --event-type member.upserted --like-error "not found"

Usage (inside the backend container / venv, same env as the app)::

    python -m src.services.sync_provision_gaps                    # provision all gap students
    python -m src.services.sync_provision_gaps --platform ielts   # IELTS only
    python -m src.services.sync_provision_gaps --platform sat      # SAT/NUET only
    python -m src.services.sync_provision_gaps --dry-run           # list who WOULD be provisioned
    python -m src.services.sync_provision_gaps --limit 20          # cap the number provisioned
"""

from __future__ import annotations

import argparse
import logging
import os

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0


def _outbox_model():
    # Lazy import to avoid an import cycle (courses.models <-> services at load time).
    from src.courses.models import StudentSyncOutbox

    return StudentSyncOutbox


# --- gap discovery -----------------------------------------------------------
# A "gap" is a distinct student (by email) whose member.upserted delivery dead-lettered because
# they were not provisioned on the target platform. We read the failed outbox rows and pull the
# student identity + program straight out of the payload the membership trigger wrote:
#   payload->'student'->>'email', payload->'student'->>'name', payload->>'program_type'.
#
# Program -> platform + product:
#   ielts               -> IELTS
#   sat                 -> SAT   (product SAT)
#   nuet                -> SAT   (product NUET)
#   both sat AND nuet   -> SAT   (product BOTH)  — a student with failed rows for both programs
# A student can be a gap on more than one platform (e.g. an IELTS row and a SAT row); each platform
# is provisioned independently.


class GapStudent:
    """A distinct gap student on ONE target platform."""

    __slots__ = ("email", "name", "platform", "product")

    def __init__(self, email: str, name: str | None, platform: str, product: str | None):
        self.email = email
        self.name = name
        self.platform = platform  # "ielts" | "sat"
        self.product = product  # "SAT" | "NUET" | "BOTH" for sat; None for ielts

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        p = f" product={self.product}" if self.product else ""
        return f"<GapStudent {self.email} -> {self.platform}{p}>"


def _program_of(payload: dict) -> str:
    return str((payload or {}).get("program_type") or "").strip().lower()


def _student_of(payload: dict) -> dict:
    return (payload or {}).get("student") or {}


# Internal/test accounts that live in the LMS but must never be provisioned onto a real platform.
_TEST_LOCALPARTS = {"test", "testing", "testtest", "test.test", "employee", "ftest", "assingmentzerotest"}


def _is_test_email(email: str) -> bool:
    """True for LMS internal/test accounts (the @lms.com domain, or obvious test local-parts)."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return True
    local, _, domain = email.partition("@")
    if domain == "lms.com":
        return True
    if local in _TEST_LOCALPARTS or local.startswith("test") or "test" in local:
        return True
    return False


def find_gap_students(db: Session) -> list[GapStudent]:
    """Distinct gap students from the failed member.upserted outbox rows, grouped by platform.

    Returns one GapStudent per (email, platform). SAT product is derived from the set of failed
    SAT-family programs for that email: {sat}->SAT, {nuet}->NUET, {sat,nuet}->BOTH.
    """
    Model = _outbox_model()
    rows = db.query(Model).filter(Model.status == "failed", Model.event_type == "member.upserted").all()

    # email -> {"name": str|None, "ielts": bool, "sat_programs": set[str]}
    agg: dict[str, dict] = {}
    for row in rows:
        payload = row.payload or {}
        student = _student_of(payload)
        email = str(student.get("email") or "").strip().lower()
        if not email:
            continue  # nothing to match on; the membership call keys on email
        if _is_test_email(email):
            continue  # never provision LMS internal/test accounts onto a real platform
        program = _program_of(payload)
        entry = agg.setdefault(email, {"name": None, "ielts": False, "sat_programs": set()})
        # Keep the first non-empty name we see for this student.
        if entry["name"] is None:
            name = student.get("name")
            entry["name"] = name.strip() if isinstance(name, str) and name.strip() else None
        if program == "ielts":
            entry["ielts"] = True
        elif program in ("sat", "nuet"):
            entry["sat_programs"].add(program)
        # Any other program (e.g. general_english) has no target platform — skip it.

    gaps: list[GapStudent] = []
    for email in sorted(agg):
        entry = agg[email]
        if entry["ielts"]:
            gaps.append(GapStudent(email, entry["name"], "ielts", None))
        sat_programs = entry["sat_programs"]
        if sat_programs:
            product = _sat_product(sat_programs)
            gaps.append(GapStudent(email, entry["name"], "sat", product))
    return gaps


def _sat_product(programs: set[str]) -> str:
    has_sat = "sat" in programs
    has_nuet = "nuet" in programs
    if has_sat and has_nuet:
        return "BOTH"
    if has_nuet:
        return "NUET"
    return "SAT"


# --- provisioning targets ----------------------------------------------------


def _ielts_base() -> str:
    return os.getenv("IELTS_SYNC_URL", "").rstrip("/")


def _sat_base() -> str:
    return os.getenv("SAT_SYNC_URL", "https://api.mastereducation.kz/api/lms").rstrip("/")


def _provision_one(gap: GapStudent, *, timeout: float = _TIMEOUT) -> tuple[str, str]:
    """POST one gap student to its target provisioning endpoint.

    Returns (outcome, detail) where outcome is one of:
      "created" — a new platform-local account was created (HTTP 201, created=true)
      "exists"  — the account already existed, returned untouched (HTTP 200, created=false)
      "error"   — misconfiguration / transport / non-2xx (detail says why)
    Idempotent: re-running is safe (an existing account is returned untouched, no email).
    """
    if gap.platform == "ielts":
        base = _ielts_base()
        key = os.getenv("IELTS_API_KEY", "")
        # IELTS router is mounted at /lms and IELTS_SYNC_URL already carries it; the route is
        # /students/ (trailing slash matters — the endpoint is declared with it).
        url = f"{base}/students/"
        body = {
            "email": gap.email,
            "full_name": gap.name,
            "send_credentials": False,  # SSO students: do not email credentials
        }
    else:  # "sat"
        base = _sat_base()
        key = os.getenv("MASTEREDU_API_KEY", "")
        url = f"{base}/students"
        body = {
            "email": gap.email,
            "full_name": gap.name,
            "product": gap.product,
            "send_credentials": False,  # SSO students: do not email credentials
        }

    if not base:
        return "error", f"{gap.platform}: base URL not configured"
    if not key:
        return "error", f"{gap.platform}: api key not configured"

    try:
        resp = httpx.post(
            url,
            json=body,
            headers={"X-API-Key": key, "Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return "error", f"{gap.platform}: transport error: {exc}"

    if resp.status_code in (200, 201):
        created = _created_flag(resp)
        if created is True or resp.status_code == 201:
            return "created", f"{gap.platform}: created {gap.email}"
        return "exists", f"{gap.platform}: exists {gap.email}"
    return "error", f"{gap.platform}: HTTP {resp.status_code}: {resp.text[:160]}"


def _created_flag(resp: httpx.Response):
    """Read the body's `created` bool; None if the body isn't the expected JSON."""
    try:
        data = resp.json()
    except Exception:
        return None
    if isinstance(data, dict) and "created" in data:
        return bool(data["created"])
    return None


# --- single-student provisioning (shared with the admin "provision" button) ---
# The batch CLI above discovers gaps from failed outbox rows. The admin endpoint needs the same
# create-or-fetch call for ONE chosen student, plus a re-emit of that student's memberships so the
# now-existing account gets linked to their groups on the platform (their original member.upserted
# had 404-dead-lettered before the account existed). Factored here so the button and the CLI share
# the exact wire contract and never drift on the /students/ payload.

_SAT_PROGRAMS = ("sat", "nuet")
_PLATFORM_PROGRAMS = {"ielts": ("ielts",), "sat": _SAT_PROGRAMS}


def platform_configured(platform: str) -> bool:
    """True when the target platform has both a base URL and an API key set."""
    if platform == "ielts":
        return bool(_ielts_base()) and bool(os.getenv("IELTS_API_KEY", ""))
    if platform == "sat":
        return bool(_sat_base()) and bool(os.getenv("MASTEREDU_API_KEY", ""))
    return False


def sat_product_for_student(db: Session, student_id: int) -> str:
    """Derive the SAT product (SAT|NUET|BOTH) from the student's SAT/NUET group memberships;
    defaults to SAT when they are in no SAT-family group yet (operator can still provision)."""
    from sqlalchemy import func

    from src.courses.models import Group, GroupStudent

    rows = (
        db.query(Group.program_type)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(GroupStudent.student_id == student_id, func.lower(Group.program_type).in_(_SAT_PROGRAMS))
        .distinct()
        .all()
    )
    programs = {str(p or "").strip().lower() for (p,) in rows}
    return _sat_product(programs) if programs else "SAT"


def provision_student_account(
    email: str, name: str | None, platform: str, product: str | None = None
) -> tuple[str, str]:
    """Create (or fetch, idempotently) one student on a platform. Returns (outcome, detail) with
    outcome in created|exists|error. Thin wrapper over ``_provision_one`` so callers don't build a
    GapStudent by hand."""
    return _provision_one(GapStudent(email, name, platform, product))


def reemit_student_memberships(db: Session, student_id: int, platform: str) -> int:
    """Touch the student's group_students rows for the platform's programs so the member trigger
    (p14, UPDATE) re-emits member.upserted — linking the just-provisioned account to its groups.
    Returns the number of membership rows touched. No-op on a DB without the trigger (SQLite tests)."""
    from sqlalchemy import bindparam, text

    programs = _PLATFORM_PROGRAMS.get(platform)
    if not programs:
        return 0
    stmt = text(
        """
        UPDATE group_students SET student_id = student_id
        WHERE student_id = :sid
          AND group_id IN (SELECT id FROM groups WHERE lower(program_type) IN :programs)
        """
    ).bindparams(bindparam("programs", expanding=True))
    result = db.execute(stmt, {"sid": student_id, "programs": list(programs)})
    db.commit()
    return result.rowcount or 0


# --- driver ------------------------------------------------------------------


def provision_gaps(
    db: Session,
    *,
    platform: str = "all",
    dry_run: bool = False,
    limit: int | None = None,
    sleep_seconds: float = 0.6,
) -> dict:
    """Provision the missing platform-local accounts for gap students.

    ``platform`` is "ielts", "sat" or "all". ``dry_run`` lists who would be provisioned and makes
    NO HTTP calls. ``limit`` caps how many gap students are processed (after filtering/ordering).
    Returns a result dict with per-platform and total tallies.
    """
    platform = (platform or "all").strip().lower()
    if platform not in ("ielts", "sat", "all"):
        raise ValueError(f"platform must be ielts|sat|all, got {platform!r}")

    gaps = find_gap_students(db)
    if platform != "all":
        gaps = [g for g in gaps if g.platform == platform]
    if limit is not None:
        gaps = gaps[:limit]

    result: dict = {
        "platform": platform,
        "dry_run": dry_run,
        "total": len(gaps),
        "created": 0,
        "exists": 0,
        "error": 0,
        "by_platform": {
            "ielts": {"total": 0, "created": 0, "exists": 0, "error": 0},
            "sat": {"total": 0, "created": 0, "exists": 0, "error": 0},
        },
        "errors": [],
    }

    for gap in gaps:
        result["by_platform"][gap.platform]["total"] += 1

    if dry_run:
        result["would_provision"] = [
            {
                "email": g.email,
                "name": g.name,
                "platform": g.platform,
                "product": g.product,
            }
            for g in gaps
        ]
        return result

    # Pace requests: IELTS enforces 120/min and SAT may throttle too. A small inter-request sleep
    # keeps a large run comfortably under the limit (~100/min at 0.6s) so nothing is dropped to 429.
    import time as _time

    for i, gap in enumerate(gaps):
        if i and sleep_seconds:
            _time.sleep(sleep_seconds)
        outcome, detail = _provision_one(gap)
        result[outcome] += 1
        result["by_platform"][gap.platform][outcome] += 1
        if outcome == "error":
            result["errors"].append(detail)
        else:
            logger.info("provision: %s", detail)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision platform-local accounts for dead-lettered gap students"
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
        help="cap the number of gap students processed",
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
        result = provision_gaps(
            db, platform=args.platform, dry_run=args.dry_run, limit=args.limit,
            sleep_seconds=args.sleep,
        )
        print("provision:", result)
        print(
            "\nnext step (not run by this tool): re-queue the dead-lettered memberships so the "
            "drainer attaches them:\n"
            "  python -m src.services.sync_replay --replay "
            '--event-type member.upserted --like-error "not found"'
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
