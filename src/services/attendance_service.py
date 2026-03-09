"""
AttendanceService — single source of truth for lesson attendance.

Abstracts over two lesson sources:
- event_id      : lessons created by the Schedule Generator (current flow)
- lesson_schedule_id : legacy LessonSchedule-based lessons (table currently empty)

All code that previously read/wrote EventParticipant for attendance should
use this service instead.

Status mapping (EventParticipant.registration_status → Attendance.status):
    "attended"   → "present"
    "late"       → "late"
    "missed"     → "absent"
    "absent"     → "absent"
    "registered" → "registered"  (not yet marked; treated as absent in reports)
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.events.models import Attendance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EP_STATUS_TO_ATTENDANCE: Dict[str, str] = {
    "attended": "present",
    "late": "late",
    "missed": "absent",
    "absent": "absent",
    "registered": "registered",
}


def ep_status_to_attendance_status(registration_status: str) -> str:
    """Convert EventParticipant.registration_status to Attendance.status."""
    return _EP_STATUS_TO_ATTENDANCE.get(registration_status, "registered")


_ATTENDANCE_TO_UI_STATUS: Dict[str, str] = {
    "present": "attended",
    "late": "late",
    "absent": "missed",
    "registered": "registered",
}


def attendance_status_to_ui(status: Optional[str]) -> str:
    """
    Convert canonical Attendance.status to UI/legacy-friendly status.

    UI pages still expect: attended | late | missed | registered.
    """
    if status is None:
        return "registered"
    return _ATTENDANCE_TO_UI_STATUS.get(status, "registered")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class AttendanceService:
    """Static-method service for Attendance CRUD operations."""

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @staticmethod
    def get_by_event(db: Session, event_id: int) -> List[Attendance]:
        """Return all Attendance records for a given event."""
        return (
            db.query(Attendance)
            .filter(Attendance.event_id == event_id)
            .all()
        )

    @staticmethod
    def get_by_event_and_user(
        db: Session, event_id: int, user_id: int
    ) -> Optional[Attendance]:
        """Return a single Attendance record for (event, user)."""
        return (
            db.query(Attendance)
            .filter(
                Attendance.event_id == event_id,
                Attendance.user_id == user_id,
            )
            .first()
        )

    @staticmethod
    def get_attendance_map_for_events(
        db: Session,
        event_ids: List[int],
        student_ids: List[int],
    ) -> Dict[Tuple[int, int], Dict]:
        """
        Return a lookup dict: (user_id, event_id) → {status, score, activity_score}.

        Used by leaderboard and full-attendance matrix endpoints.
        """
        if not event_ids or not student_ids:
            return {}

        rows = (
            db.query(Attendance)
            .filter(
                Attendance.event_id.in_(event_ids),
                Attendance.user_id.in_(student_ids),
            )
            .all()
        )
        return {
            (row.user_id, row.event_id): {
                "status": row.status,
                "score": row.score,
                "activity_score": row.activity_score,
            }
            for row in rows
        }

    @staticmethod
    def count_for_event(db: Session, event_id: int, statuses: Optional[List[str]] = None) -> int:
        """Count attendance records for an event, optionally filtered by status."""
        query = db.query(Attendance).filter(Attendance.event_id == event_id)
        if statuses:
            query = query.filter(Attendance.status.in_(statuses))
        return query.count()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    @staticmethod
    def upsert_for_event(
        db: Session,
        event_id: int,
        user_id: int,
        status: str,
        score: int = 0,
        activity_score: Optional[float] = None,
        notes: Optional[str] = None,
        flush: bool = True,
    ) -> Attendance:
        """
        Create or update an Attendance record for (event_id, user_id).

        Does NOT commit — callers are responsible for db.commit().
        """
        record = (
            db.query(Attendance)
            .filter(
                Attendance.event_id == event_id,
                Attendance.user_id == user_id,
            )
            .first()
        )

        if record:
            record.status = status
            record.score = score
            if activity_score is not None:
                record.activity_score = activity_score
            if notes is not None:
                record.notes = notes
        else:
            record = Attendance(
                event_id=event_id,
                user_id=user_id,
                status=status,
                score=score,
                activity_score=activity_score,
                notes=notes,
            )
            db.add(record)

        if flush:
            db.flush()
        return record

    @staticmethod
    def bulk_upsert_for_event(
        db: Session,
        event_id: int,
        updates: List[Dict],
    ) -> int:
        """
        Bulk upsert attendance for a list of students.

        Each item in updates must have: user_id, status.
        Optional: score, activity_score.
        Returns count of upserted records.
        """
        count = 0
        for item in updates:
            AttendanceService.upsert_for_event(
                db=db,
                event_id=event_id,
                user_id=item["user_id"],
                status=item["status"],
                score=item.get("score", 0),
                activity_score=item.get("activity_score"),
                flush=False,
            )
            count += 1
        db.flush()
        return count

    @staticmethod
    def rebind_event_attendance(
        db: Session,
        from_event_id: int,
        to_event_id: int,
        flush: bool = True,
    ) -> int:
        """
        Safely move attendance records from one event to another.
        Only moves records for user_ids that do not already have a record in to_event.
        Returns count of records rebound.
        Does NOT commit.
        """
        if from_event_id == to_event_id:
            return 0

        existing_to = {
            row.user_id
            for row in db.query(Attendance.user_id)
            .filter(Attendance.event_id == to_event_id)
            .all()
        }

        records = db.query(Attendance).filter(
            Attendance.event_id == from_event_id,
        ).all()

        count = 0
        for rec in records:
            if rec.user_id in existing_to:
                continue
            rec.event_id = to_event_id
            existing_to.add(rec.user_id)
            count += 1

        if flush:
            db.flush()
        return count
