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

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.events.models import Attendance

#: How far ahead a lesson may be and still be markable.
#:
#: Not zero. Event datetimes are stored naive while the school runs on Almaty time (UTC+5),
#: so a strict comparison would refuse a teacher marking a class that has obviously already
#: started for them. A day of slack absorbs every timezone question and still catches the
#: failure this guards — lessons carrying marks weeks ahead of themselves.
MARKABLE_GRACE_DAYS = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EP_STATUS_TO_ATTENDANCE: Dict[str, str] = {
    "attended": "present",
    "late": "late",
    "missed": "absent",
    "absent": "absent",
    "registered": "registered",
    "cancelled": "cancelled",
}


def ep_status_to_attendance_status(registration_status: str) -> str:
    """Convert EventParticipant.registration_status to Attendance.status."""
    return _EP_STATUS_TO_ATTENDANCE.get(registration_status, "registered")


_ATTENDANCE_TO_UI_STATUS: Dict[str, str] = {
    "present": "attended",
    "late": "late",
    "absent": "missed",
    "registered": "registered",
    "cancelled": "cancelled",
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
        Return a lookup dict: (user_id, event_id) → {status, score, activity_score, excused, excuse_note}.

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
                "excused": bool(row.excused),
                "excuse_note": row.excuse_note,
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
    def event_is_unmarkable_because_future(db: Session, event_id: int) -> Optional[str]:
        """The lesson has not happened yet — reason string if so, ``None`` if it is markable.

        You cannot take a register for a class that has not met. Production carried marks on
        lessons dated weeks ahead, which read as a marking bug and was not one: the lessons
        were taught in July and then *rescheduled* into September, carrying their attendance.
        That hole is closed where it was opened (``lesson_requests``), and this is the second
        lock — the state should not be reachable by writing, either.

        The tolerance is deliberate and generous. Event datetimes are stored naive and the
        school runs on Almaty time, so a strict ``start > utcnow`` would refuse a teacher
        marking a lesson that has plainly started for them the moment any part of that
        pipeline is off by hours. A whole day of slack cannot be hit by a timezone and cannot
        hide the bug this exists to stop, which was measured in weeks.
        """
        from src.schemas.models import Event

        event = db.query(Event).filter(Event.id == event_id).first()
        if event is None or event.start_datetime is None:
            return None
        starts = event.start_datetime
        if starts.tzinfo is not None:
            starts = starts.astimezone(timezone.utc).replace(tzinfo=None)
        if starts - datetime.utcnow() <= timedelta(days=MARKABLE_GRACE_DAYS):
            return None
        return (
            f"Урок ещё не проведён — он назначен на "
            f"{starts.strftime('%d.%m.%Y %H:%M')}. Отметить посещаемость можно после занятия."
        )

    @staticmethod
    def upsert_for_event(
        db: Session,
        event_id: int,
        user_id: int,
        status: str,
        score: int = 0,
        activity_score: Optional[float] = None,
        notes: Optional[str] = None,
        excused: Optional[bool] = None,
        excuse_note: Optional[str] = None,
        excused_by_user_id: Optional[int] = None,
        flush: bool = True,
    ) -> Attendance:
        """
        Create or update an Attendance record for (event_id, user_id).

        Does NOT commit — callers are responsible for db.commit().

        ``excused`` тристабилен намеренно. ``None`` — «вызывающий про уважительность не
        знает»: таких путей в коде большинство (перепривязка урока, импорт, старые клиенты),
        и любой из них, обнуляя флаг по умолчанию, тихо снимал бы причину, которую поставил
        учитель. ``True``/``False`` — явное решение вызывающего.

        Уход статуса из «пропуска» снимает уважительность всегда, даже при ``excused=None``:
        «Ув.» превратившееся в «Был» — это отмена пропуска, и строка, оставшаяся
        уважительной при статусе present, нарушает инвариант словаря.
        """
        from src.services.attendance_status import normalize_status, validate_excused

        if excused is not None:
            validate_excused(status, excused, excuse_note)

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

        if normalize_status(status) != "absent":
            record.excused = False
            record.excuse_note = None
            record.excused_by_user_id = None
            record.excused_at = None
        elif excused is not None:
            if excused:
                new_note = excuse_note.strip() if excuse_note else None
                # Авторство переписывается только когда уважительность действительно
                # меняется: строка в неё переходит или переписана причина. Сетка сохраняет
                # колонку целиком, поэтому нетронутая уважительная строка приезжает сюда
                # при каждой правке соседней ячейки — безусловная простановка отдавала бы
                # чужую отметку последнему, кто открыл экран, и сдвигала бы дату.
                if not record.excused or new_note != record.excuse_note:
                    record.excused_by_user_id = excused_by_user_id
                    record.excused_at = datetime.now(timezone.utc).replace(tzinfo=None)
                record.excused = True
                record.excuse_note = new_note
            else:
                record.excused = False
                record.excuse_note = None
                record.excused_by_user_id = None
                record.excused_at = None

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
                excused=item.get("excused"),
                excuse_note=item.get("excuse_note"),
                excused_by_user_id=item.get("excused_by_user_id"),
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
