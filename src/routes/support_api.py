"""Read-only student context for the Support platform (service-key auth).

Mirrors the `X-API-Key` pattern in `src/admin/routes/sat_schedules.py`
(`verify_masteredu_api_key`), but with status codes tuned for a
service-to-service integration: missing server config -> 503 (misconfigured,
not the caller's fault), wrong/missing key -> 401.
"""
import os
import secrets
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from src.config import get_db
from src.schemas.models import (
    UserInDB,
    Group,
    GroupStudent,
    StudentCourseSummary,
    Attendance,
    Event,
    EventGroup,
    Assignment,
    AssignmentSubmission,
)
from src.services.attendance_service import attendance_status_to_ui
from src.trials.services import earliest_active_expiry

router = APIRouter()

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def verify_support_api_key(api_key: str = Security(api_key_header)) -> str:
    """Auth dependency for /support-api/*.

    Missing `SUPPORT_API_KEY` server-side config -> 503 (nothing the caller can
    fix). Missing/incorrect header -> 401. Uses `secrets.compare_digest` to
    avoid a timing side-channel on key comparison.
    """
    expected = os.getenv("SUPPORT_API_KEY")
    if not expected:
        raise HTTPException(status_code=503, detail="SUPPORT_API_KEY is not configured")
    if not api_key or not secrets.compare_digest(api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return api_key


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _distinct_curator_emails(db: Session, student_id: int) -> List[str]:
    """Distinct, lowercased curator emails across every group the student belongs to."""
    rows = (
        db.query(UserInDB.email)
        .join(Group, Group.curator_id == UserInDB.id)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(GroupStudent.student_id == student_id, UserInDB.email.isnot(None))
        .distinct()
        .all()
    )
    emails = {row[0].strip().lower() for row in rows if row[0]}
    return sorted(emails)


def _person(user: Optional[UserInDB]) -> Optional[dict]:
    if not user:
        return None
    return {"name": user.name, "email": _normalize_email(user.email)}


@router.get("/users/{email}", dependencies=[Depends(verify_support_api_key)])
def get_user_summary(email: str, db: Session = Depends(get_db)):
    """Minimal user lookup for the Support platform: role, name, active flag,
    and the curator emails of every group the user belongs to (as a student)."""
    normalized = _normalize_email(email)
    user = db.query(UserInDB).filter(func.lower(UserInDB.email) == normalized).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "role": user.role,
        "full_name": user.name,
        "is_active": user.is_active,
        "curator_emails": _distinct_curator_emails(db, user.id),
    }


@router.get("/students/{email}/context", dependencies=[Depends(verify_support_api_key)])
def get_student_context(email: str, db: Session = Depends(get_db)):
    """Compact, AI-consumable snapshot of a student for the Support platform.

    Aggregates are sourced from the same models/queries used elsewhere in the
    LMS (curator student-journal profile, student_course_summaries, the
    Attendance/AssignmentSubmission tables, and the trials service) rather than
    hand-rolled joins. Two simplifications, called out explicitly:
      - homework.pending_count only counts group-scoped assignments
        (Assignment.group_id) that the student hasn't submitted yet. It does
        not re-derive the full per-assignment visibility computation in
        src/assignments/routes/assignments.py (course access / weekly release
        schedule / lesson-linked assignments), which would require an
        expensive per-assignment check.
      - attendance.rate_pct is computed over the student's full Attendance
        history (event-based rows only; legacy LessonSchedule-based rows are
        not included), matching src/curator/routes/student_journal.py.
    """
    normalized = _normalize_email(email)
    student = (
        db.query(UserInDB)
        .filter(func.lower(UserInDB.email) == normalized, UserInDB.role == "student")
        .first()
    )
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # --- Groups (with curator/teacher) ---
    group_rows = (
        db.query(Group)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .options(joinedload(Group.curator), joinedload(Group.teacher))
        .filter(GroupStudent.student_id == student.id)
        .all()
    )
    group_ids = [g.id for g in group_rows]

    groups_data = [
        {
            "name": g.name,
            "program_type": g.program_type,
            "curator": _person(g.curator),
            "teacher": _person(g.teacher),
        }
        for g in group_rows
    ]

    # --- Progress (student_course_summaries — precomputed, not re-aggregated here) ---
    summary_rows = (
        db.query(StudentCourseSummary)
        .options(joinedload(StudentCourseSummary.course))
        .filter(StudentCourseSummary.user_id == student.id)
        .all()
    )
    courses_data = [
        {
            "title": s.course.title if s.course else None,
            "completion_pct": s.completion_percentage,
            "avg_score": s.average_assignment_percentage,
        }
        for s in summary_rows
    ]

    # --- Attendance (Attendance is the single source of truth; same filters as
    # src/curator/routes/student_journal.py's list/profile endpoints) ---
    att_total = (
        db.query(func.count(Attendance.id))
        .filter(
            Attendance.user_id == student.id,
            Attendance.event_id.isnot(None),
            Attendance.status != "cancelled",
        )
        .scalar()
        or 0
    )
    att_attended = (
        db.query(func.count(Attendance.id))
        .filter(
            Attendance.user_id == student.id,
            Attendance.event_id.isnot(None),
            Attendance.status.in_(["present", "late"]),
        )
        .scalar()
        or 0
    )
    att_rate_pct = round(att_attended / att_total * 100, 1) if att_total > 0 else None

    recent_att_rows = (
        db.query(Attendance, Event)
        .join(Event, Event.id == Attendance.event_id)
        .filter(Attendance.user_id == student.id, Attendance.event_id.isnot(None))
        .order_by(Event.start_datetime.desc())
        .limit(5)
        .all()
    )
    recent_attendance = [
        {"date": ev.start_datetime.date().isoformat(), "status": attendance_status_to_ui(att.status)}
        for att, ev in recent_att_rows
    ]

    # --- Homework ---
    pending_count = 0
    if group_ids:
        assigned_ids = {
            row[0]
            for row in db.query(Assignment.id)
            .filter(
                Assignment.group_id.in_(group_ids),
                Assignment.is_active == True,
                Assignment.is_hidden == False,
            )
            .all()
        }
        if assigned_ids:
            submitted_ids = {
                row[0]
                for row in db.query(AssignmentSubmission.assignment_id)
                .filter(
                    AssignmentSubmission.user_id == student.id,
                    AssignmentSubmission.assignment_id.in_(assigned_ids),
                    AssignmentSubmission.is_hidden == False,
                )
                .all()
            }
            pending_count = len(assigned_ids - submitted_ids)

    graded_rows = (
        db.query(AssignmentSubmission, Assignment)
        .join(Assignment, Assignment.id == AssignmentSubmission.assignment_id)
        .filter(
            AssignmentSubmission.user_id == student.id,
            AssignmentSubmission.is_graded == True,
            AssignmentSubmission.score.isnot(None),
        )
        .order_by(AssignmentSubmission.graded_at.desc())
        .limit(5)
        .all()
    )
    recent_grades = [{"title": a.title, "grade": s.score} for s, a in graded_rows]

    # --- Upcoming lessons (same Event/EventGroup join as src/admin/routes/sat_schedules.py) ---
    upcoming_rows = []
    if group_ids:
        upcoming_rows = (
            db.query(Event)
            .join(EventGroup, EventGroup.event_id == Event.id)
            .filter(
                EventGroup.group_id.in_(group_ids),
                Event.is_active == True,
                Event.start_datetime >= now,
            )
            .order_by(Event.start_datetime.asc())
            .limit(3)
            .all()
        )
    upcoming_lessons = [
        {"title": ev.title, "starts_at": ev.start_datetime.isoformat()} for ev in upcoming_rows
    ]

    # --- Trial (src/trials/services.earliest_active_expiry) ---
    trial_expires_at = earliest_active_expiry(db, student.id)

    return {
        "profile": {
            "name": student.name,
            "email": _normalize_email(student.email),
            "student_id": student.student_id,
        },
        "groups": groups_data,
        "progress": {"courses": courses_data},
        "attendance": {"rate_pct": att_rate_pct, "recent": recent_attendance},
        "homework": {"pending_count": pending_count, "recent_grades": recent_grades},
        "upcoming_lessons": upcoming_lessons,
        "trial": {
            "is_trial": student.is_trial,
            "expires_at": trial_expires_at.isoformat() if trial_expires_at else None,
        },
        "activity": {
            "streak": student.daily_streak,
            "last_active": student.last_activity_date.isoformat() if student.last_activity_date else None,
        },
    }
