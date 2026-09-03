"""Structured per-track student targets (Platform Integration Pack §6.4, E5).

* ielts: bands in 0.5 steps between 4.0 and 9.0 — ``overall`` plus optional ``listening``,
  ``reading``, ``writing``, ``speaking``;
* sat: ``total`` 400–1600 and optional ``math`` / ``verbal`` 200–800, all in steps of 10;
* nuet: ``total`` 0–120.

Students set their own, group staff and admins may override (``source``/``set_by`` keep that
visible), parents read. The legacy free-text IELTS target from Assignment Zero migrates through
:func:`migrate_assignment_zero_targets` (same rule as the p20 data migration).
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.integrations.models import StudentTarget

_TRUTHY = {"1", "true", "yes", "on"}
TRACKS = ("sat", "ielts", "nuet")
IELTS_MODULES = ("listening", "reading", "writing", "speaking")
IELTS_KEYS = ("overall",) + IELTS_MODULES
IELTS_MIN, IELTS_MAX = 4.0, 9.0
SAT_RANGES = {"total": (400, 1600), "math": (200, 800), "verbal": (200, 800)}
NUET_RANGES = {"total": (0, 120)}

_BAND_RE = re.compile(r"^\d(?:\.\d)?$")


class TargetsError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def enabled() -> bool:
    return os.getenv("PLATFORM_TARGETS_ENABLED", "").strip().lower() in _TRUTHY


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- bands -----------------------------------------------------------------------------

def is_half_step(value: float) -> bool:
    return abs(value * 2 - round(value * 2)) < 1e-9


def parse_band(text: Optional[str]) -> Optional[float]:
    """Legacy free text → band, or None when it is not a plain band ("7.5", "7,5", "band 7", "8+")."""
    if text is None:
        return None
    value = str(text).strip().lower()
    value = re.sub(r"^band\s*", "", value).rstrip("+").strip().replace(",", ".")
    if not _BAND_RE.match(value):
        return None
    band = float(value)
    if not (IELTS_MIN <= band <= IELTS_MAX) or not is_half_step(band):
        return None
    return band


def ielts_overall(bands: list[float]) -> float:
    """IELTS overall rounding: mean of the bands; fraction < .25 → .0, < .75 → .5, else next whole."""
    mean = sum(bands) / len(bands)
    whole = math.floor(mean)
    frac = mean - whole
    if frac < 0.25:
        return float(whole)
    if frac < 0.75:
        return whole + 0.5
    return float(whole + 1)


# --- validation ---------------------------------------------------------------------------

def _number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TargetsError(f"{key} must be a number")
    return float(value)


def validate_targets(track: str, payload: Any) -> dict:
    track = (track or "").strip().lower()
    if track not in TRACKS:
        raise TargetsError("unknown track")
    if not isinstance(payload, dict) or not payload:
        raise TargetsError("targets must be a non-empty object")
    out: dict = {}
    if track == "ielts":
        for key, value in payload.items():
            if key not in IELTS_KEYS:
                raise TargetsError(f"unknown IELTS target '{key}'")
            band = _number(value, key)
            if not (IELTS_MIN <= band <= IELTS_MAX) or not is_half_step(band):
                raise TargetsError(f"{key} must be a band between 4.0 and 9.0 in steps of 0.5")
            out[key] = band
        return out
    ranges = SAT_RANGES if track == "sat" else NUET_RANGES
    for key, value in payload.items():
        if key not in ranges:
            raise TargetsError(f"unknown {track.upper()} target '{key}'")
        number = _number(value, key)
        low, high = ranges[key]
        if number != int(number) or not (low <= number <= high) or (track == "sat" and int(number) % 10):
            step = " in steps of 10" if track == "sat" else ""
            raise TargetsError(f"{key} must be a whole number between {low} and {high}{step}")
        out[key] = int(number)
    return out


# --- storage ---------------------------------------------------------------------------------

def _row_dict(row: StudentTarget) -> dict:
    return {
        "track": row.track,
        "targets": dict(row.targets or {}),
        "note": row.note,
        "source": row.source,
        "set_by": row.set_by,
        "updated_at": row.updated_at.replace(tzinfo=timezone.utc).isoformat() if row.updated_at else None,
    }


def get_targets(db: Session, user_id: int) -> dict:
    rows = db.query(StudentTarget).filter(StudentTarget.user_id == user_id).all()
    return {row.track: _row_dict(row) for row in rows}


def set_target(db: Session, user_id: int, track: str, payload: Any, *, source: str, set_by: Optional[int],
               note: Optional[str] = None) -> dict:
    track = (track or "").strip().lower()
    targets = validate_targets(track, payload)
    row = (
        db.query(StudentTarget)
        .filter(StudentTarget.user_id == user_id, StudentTarget.track == track)
        .first()
    )
    if row is None:
        row = StudentTarget(user_id=user_id, track=track)
        db.add(row)
    row.targets = targets
    row.source = source
    row.set_by = set_by
    if note is not None:
        row.note = note or None
    row.updated_at = _utcnow()
    db.commit()
    return _row_dict(row)


def migrate_assignment_zero_targets(db: Session) -> dict:
    """Copy the legacy free-text IELTS target into student_targets (idempotent; existing rows win)."""
    from src.assignments.models import AssignmentZeroSubmission

    out = {"migrated": 0, "noted": 0, "skipped": 0}
    existing = {
        row.user_id for row in db.query(StudentTarget.user_id).filter(StudentTarget.track == "ielts").all()
    }
    submissions = db.query(AssignmentZeroSubmission).order_by(AssignmentZeroSubmission.id.asc()).all()
    for sub in submissions:
        text = (sub.ielts_target_score or "").strip()
        if not text or sub.user_id in existing:
            out["skipped"] += 1
            continue
        band = parse_band(text)
        db.add(StudentTarget(
            user_id=sub.user_id, track="ielts", source="assignment_zero", set_by=None,
            targets={"overall": band} if band is not None else {},
            note=None if band is not None else text,
        ))
        existing.add(sub.user_id)
        out["migrated" if band is not None else "noted"] += 1
    db.commit()
    return out
