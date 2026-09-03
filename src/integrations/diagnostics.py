"""IELTS diagnostic entry bands (Platform Integration Pack §2.6) — the "start" of the targets tile.

Fetched by the nightly job (and once when the feature is enabled) in batches of ≤500 through
``POST /api/lms/students/batch-diagnostic`` and stored per student in ``platform_diagnostics``;
a student request only ever reads the stored row. Gated by ``PLATFORM_TARGETS_ENABLED``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.integrations.models import PlatformDiagnostic
from src.integrations.targets import enabled

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
MODULES = ("listening", "reading", "writing")

FetchFn = Callable[[list[dict]], Optional[dict]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def normalise(diagnostic: Optional[dict]) -> Optional[dict]:
    """Platform camelCase → the stored shape. A module block with band null is "taken, not scored"."""
    if not diagnostic:
        return None
    out: dict[str, Any] = {}
    for module in MODULES:
        block = diagnostic.get(module)
        out[module] = (
            {"band": block.get("band"), "completed_at": block.get("completedAt"), "result_url": block.get("resultUrl")}
            if isinstance(block, dict) else None
        )
    out["completed_count"] = diagnostic.get("completedCount")
    out["overall"] = diagnostic.get("overallBand")
    return out


def ielts_track_students(db: Session) -> list:
    """Active students in active, non-special IELTS groups (same rule as the platform tiles)."""
    from src.auth.models import UserInDB
    from src.courses.models import Group, GroupStudent

    return (
        db.query(UserInDB)
        .join(GroupStudent, GroupStudent.student_id == UserInDB.id)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(
            func.lower(Group.program_type) == "ielts",
            or_(Group.is_active.is_(True), Group.is_active.is_(None)),
            or_(Group.is_special.is_(False), Group.is_special.is_(None)),
            UserInDB.is_active.is_(True),
            UserInDB.email.isnot(None),
        )
        .distinct()
        .order_by(UserInDB.id.asc())
        .all()
    )


def _default_fetch(students: list[dict]) -> Optional[dict]:
    from src.services.ielts_service import IELTSService

    return asyncio.run(IELTSService.fetch_batch_diagnostic(students))


def refresh_diagnostics(db: Session, *, fetch: Optional[FetchFn] = None, now: Optional[datetime] = None) -> dict:
    out = {"students": 0, "batches": 0, "stored": 0, "none": 0, "errors": 0}
    if not enabled():
        return out
    fetch = fetch or _default_fetch
    now = now or _utcnow()
    students = [s for s in ielts_track_students(db) if s.email and "@" in s.email]
    out["students"] = len(students)
    by_email = {s.email.strip().lower(): s for s in students}
    existing = {row.user_id: row for row in db.query(PlatformDiagnostic).filter(PlatformDiagnostic.platform == "ielts").all()}

    for start in range(0, len(students), BATCH_SIZE):
        batch = students[start:start + BATCH_SIZE]
        payload = []
        for s in batch:
            entry: dict[str, Any] = {"email": s.email.strip().lower()}
            if s.central_auth_user_id:
                entry["central_auth_user_id"] = s.central_auth_user_id
            payload.append(entry)
        out["batches"] += 1
        try:
            response = fetch(payload) or {}
        except Exception as exc:  # noqa: BLE001 - one bad batch must not stop the others
            out["errors"] += 1
            logger.warning("diagnostics: batch %s failed: %s", out["batches"], exc)
            continue
        for item in response.get("results") or []:
            student = by_email.get((item.get("email") or "").strip().lower())
            if student is None:
                continue
            normalised = normalise(item.get("diagnostic"))
            row = existing.get(student.id)
            if row is None:
                row = PlatformDiagnostic(user_id=student.id, platform="ielts")
                db.add(row)
                existing[student.id] = row
            row.payload = normalised
            row.fetched_at = now
            out["stored" if normalised else "none"] += 1
        db.commit()
    return out


def stored_start(db: Session, user_id: int, platform: str = "ielts") -> Optional[dict]:
    """The stored diagnostic for the tile, or None when there is no row / the student never took it."""
    row = (
        db.query(PlatformDiagnostic)
        .filter(PlatformDiagnostic.user_id == user_id, PlatformDiagnostic.platform == platform)
        .first()
    )
    if row is None or not row.payload:
        return None
    return {**row.payload, "fetched_at": _iso(row.fetched_at)}
