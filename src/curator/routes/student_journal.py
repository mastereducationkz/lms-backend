"""
Student Journal API routes.

Endpoints:
  GET /student-journal/list       - Paginated list of students with aggregated metrics
  GET /student-journal/{id}/profile - Full profile for a single student
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, case, and_, desc
from typing import List, Optional
from datetime import datetime, timezone

from src.config import get_db
from src.schemas.models import (
    UserInDB, Group, GroupStudent,
    EventParticipant, Event, EventGroup,
    AssignmentSubmission, Assignment,
    StudentProgress,
    AssignmentZeroSubmission,
)
from src.routes.auth import get_current_user_dependency
from src.schemas.models import Attendance
from src.services.attendance_service import attendance_status_to_ui
from src.utils.permissions import require_role

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_allowed_group_ids(current_user: UserInDB, db: Session) -> Optional[List[int]]:
    """Return group_ids the current user may see, or None meaning 'all'."""
    # head_teacher sees every student, same as the student report access matrix.
    if current_user.role in ("admin", "head_curator", "head_teacher"):
        return None  # unrestricted
    if current_user.role == "curator":
        # Archived groups stay visible to their curator — a finished cohort's
        # students and analytics must not vanish the day the group is archived.
        groups = db.query(Group.id).filter(
            Group.curator_id == current_user.id,
        ).all()
        return [g[0] for g in groups]
    return []


def _build_student_query(db: Session, allowed_group_ids: Optional[List[int]], group_id: Optional[int],
                         search: Optional[str], include_archived: bool = False):
    """Return a base query of (UserInDB, GroupStudent, Group) with access filters applied."""
    q = (
        db.query(UserInDB, GroupStudent, Group)
        .join(GroupStudent, GroupStudent.student_id == UserInDB.id)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(UserInDB.role == "student", UserInDB.is_active == True)
    )
    # A search must find the student even if their group was archived; the
    # active-only default exists to keep the browsing view uncluttered, not to
    # hide people.
    if not include_archived and not search:
        q = q.filter(Group.is_active == True)
    if allowed_group_ids is not None:
        q = q.filter(GroupStudent.group_id.in_(allowed_group_ids))
    if group_id:
        q = q.filter(GroupStudent.group_id == group_id)
    if search:
        # Staff paste whatever identifier they have — a name, an email, a student id.
        pattern = f"%{search.strip()}%"
        q = q.filter(
            (UserInDB.name.ilike(pattern))
            | (UserInDB.email.ilike(pattern))
            | (UserInDB.student_id.ilike(pattern))
        )
    return q


# ---------------------------------------------------------------------------
# GET /student-journal/list
# ---------------------------------------------------------------------------

@router.get("/list", summary="List students with aggregated metrics")
def list_students(
    group_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    include_archived: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: UserInDB = Depends(require_role(["curator", "head_curator", "admin", "head_teacher"])),
    db: Session = Depends(get_db),
):
    allowed_group_ids = _get_allowed_group_ids(current_user, db)
    q = _build_student_query(db, allowed_group_ids, group_id, search, include_archived)

    total = q.with_entities(func.count(UserInDB.id.distinct())).scalar()
    rows = q.distinct(UserInDB.id).order_by(UserInDB.id).offset(offset).limit(limit).all()

    student_ids = [r[0].id for r in rows]

    # --- Aggregate attendance from Attendance (single source of truth) ---
    att_total = (
        db.query(Attendance.user_id, func.count(Attendance.id).label("total"))
        .filter(
            Attendance.user_id.in_(student_ids),
            Attendance.event_id.isnot(None),
            # Cancelled lessons must not count toward a student's attendance rate.
            Attendance.status != "cancelled",
        )
        .group_by(Attendance.user_id)
        .all()
    )
    att_attended = (
        db.query(Attendance.user_id, func.count(Attendance.id).label("attended"))
        .filter(
            Attendance.user_id.in_(student_ids),
            Attendance.event_id.isnot(None),
            Attendance.status.in_(["present", "late"]),
        )
        .group_by(Attendance.user_id)
        .all()
    )
    att_total_map = {r.user_id: r.total for r in att_total}
    att_attended_map = {r.user_id: r.attended for r in att_attended}

    # --- Aggregate LMS progress ---
    # Course-level records (lesson_id IS NULL) contain overall course progress
    lms_rows = (
        db.query(StudentProgress.user_id, func.avg(StudentProgress.completion_percentage).label("avg_pct"))
        .filter(
            StudentProgress.user_id.in_(student_ids),
            StudentProgress.lesson_id.is_(None),
            StudentProgress.assignment_id.is_(None),
        )
        .group_by(StudentProgress.user_id)
        .all()
    )
    lms_map = {r.user_id: round(float(r.avg_pct), 1) for r in lms_rows}

    # --- Aggregate homework ---
    hw_rows = (
        db.query(
            AssignmentSubmission.user_id,
            func.count(AssignmentSubmission.id).label("submitted"),
            func.avg(
                case((AssignmentSubmission.is_graded == True, AssignmentSubmission.score), else_=None)
            ).label("avg_score"),
        )
        .filter(AssignmentSubmission.user_id.in_(student_ids))
        .group_by(AssignmentSubmission.user_id)
        .all()
    )
    hw_map = {r.user_id: {"submitted": r.submitted, "avg_score": round(float(r.avg_score), 1) if r.avg_score else None} for r in hw_rows}

    # --- Assignment Zero status ---
    az_rows = (
        db.query(AssignmentZeroSubmission.user_id, AssignmentZeroSubmission.is_draft)
        .filter(AssignmentZeroSubmission.user_id.in_(student_ids))
        .all()
    )
    az_map = {r.user_id: r.is_draft for r in az_rows}

    result = []
    for user, gs, group in rows:
        sid = user.id
        total_att = att_total_map.get(sid, 0)
        attended_att = att_attended_map.get(sid, 0)
        att_pct = round(attended_att / total_att * 100, 1) if total_att > 0 else None

        az_is_draft = az_map.get(sid)
        if sid not in az_map:
            az_status = "not_started"
        elif az_is_draft:
            az_status = "draft"
        else:
            az_status = "submitted"

        last_activity = None
        if user.last_activity_date:
            last_activity = datetime.combine(user.last_activity_date, datetime.min.time()).isoformat()

        result.append({
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "avatar_url": user.avatar_url,
            "group_id": group.id,
            "group_name": group.name,
            "attendance_attended": attended_att,
            "attendance_total": total_att,
            "attendance_rate": att_pct,
            "lms_progress": lms_map.get(sid),
            "hw_submitted": hw_map.get(sid, {}).get("submitted", 0),
            "hw_avg_score": hw_map.get(sid, {}).get("avg_score"),
            "az_status": az_status,
            "last_activity": last_activity,
        })

    return {"total": total, "students": result}


# ---------------------------------------------------------------------------
# GET /student-journal/groups
# ---------------------------------------------------------------------------

@router.get("/groups", summary="List groups accessible to current user")
def list_groups(
    include_archived: bool = Query(False),
    current_user: UserInDB = Depends(require_role(["curator", "head_curator", "admin", "head_teacher"])),
    db: Session = Depends(get_db),
):
    allowed_group_ids = _get_allowed_group_ids(current_user, db)
    q = db.query(Group)
    if not include_archived:
        q = q.filter(Group.is_active == True)
    if allowed_group_ids is not None:
        q = q.filter(Group.id.in_(allowed_group_ids))
    groups = q.order_by(Group.name).all()
    return [{"id": g.id, "name": g.name, "is_archived": not g.is_active} for g in groups]


# ---------------------------------------------------------------------------
# GET /student-journal/{student_id}/profile
# ---------------------------------------------------------------------------

@router.get("/{student_id}/profile", summary="Full profile for a student")
def get_student_profile(
    student_id: int,
    current_user: UserInDB = Depends(require_role(["curator", "head_curator", "admin", "head_teacher"])),
    db: Session = Depends(get_db),
):
    student = db.query(UserInDB).filter(UserInDB.id == student_id, UserInDB.role == "student").first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # Access check: curator can only see their own students
    if current_user.role == "curator":
        curator_group_ids = [
            g[0] for g in db.query(Group.id).filter(Group.curator_id == current_user.id, Group.is_active == True).all()
        ]
        in_group = db.query(GroupStudent).filter(
            GroupStudent.student_id == student_id,
            GroupStudent.group_id.in_(curator_group_ids),
        ).first()
        if not in_group:
            raise HTTPException(status_code=403, detail="Access denied")

    # --- Groups ---
    group_memberships = (
        db.query(GroupStudent, Group)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(GroupStudent.student_id == student_id)
        .all()
    )
    groups_data = [{"id": g.id, "name": g.name} for _, g in group_memberships]

    # --- Assignment Zero ---
    az = db.query(AssignmentZeroSubmission).filter(AssignmentZeroSubmission.user_id == student_id).first()
    az_data = None
    if az:
        az_data = {
            "is_draft": az.is_draft,
            "full_name": az.full_name,
            "phone_number": az.phone_number,
            "parent_phone_number": az.parent_phone_number,
            "telegram_id": az.telegram_id,
            "email": az.email,
            "college_board_email": az.college_board_email,
            "college_board_password": az.college_board_password,
            "birthday_date": az.birthday_date.isoformat() if az.birthday_date else None,
            "city": az.city,
            "school_type": az.school_type,
            "group_name": az.group_name,
            # SAT
            "sat_target_date": az.sat_target_date,
            "has_passed_sat_before": az.has_passed_sat_before,
            "previous_sat_score": az.previous_sat_score,
            "recent_practice_test_score": az.recent_practice_test_score,
            "bluebook_practice_test_5_score": az.bluebook_practice_test_5_score,
            "screenshot_url": az.screenshot_url,
            # Grammar
            "grammar_punctuation": az.grammar_punctuation,
            "grammar_noun_clauses": az.grammar_noun_clauses,
            "grammar_relative_clauses": az.grammar_relative_clauses,
            "grammar_verb_forms": az.grammar_verb_forms,
            "grammar_comparisons": az.grammar_comparisons,
            "grammar_transitions": az.grammar_transitions,
            "grammar_synthesis": az.grammar_synthesis,
            # Reading
            "reading_word_in_context": az.reading_word_in_context,
            "reading_text_structure": az.reading_text_structure,
            "reading_cross_text": az.reading_cross_text,
            "reading_central_ideas": az.reading_central_ideas,
            "reading_inferences": az.reading_inferences,
            # Passages
            "passages_literary": az.passages_literary,
            "passages_social_science": az.passages_social_science,
            "passages_humanities": az.passages_humanities,
            "passages_science": az.passages_science,
            "passages_poetry": az.passages_poetry,
            # Math
            "math_topics": az.math_topics,
            # IELTS
            "ielts_target_date": az.ielts_target_date,
            "has_passed_ielts_before": az.has_passed_ielts_before,
            "previous_ielts_score": az.previous_ielts_score,
            "ielts_target_score": az.ielts_target_score,
            "updated_at": az.updated_at.isoformat() if az.updated_at else None,
        }

    # --- Attendance (from Attendance — single source of truth) ---
    attendance_rows = (
        db.query(Attendance, Event)
        .join(Event, Event.id == Attendance.event_id)
        .filter(
            Attendance.user_id == student_id,
            Attendance.event_id.isnot(None),
        )
        .order_by(Event.start_datetime.desc())
        .limit(100)
        .all()
    )
    attendance_data = [
        {
            "event_id": att.event_id,
            "event_title": ev.title,
            "event_topic": ev.topic,
            "event_date": ev.start_datetime.isoformat(),
            "status": attendance_status_to_ui(att.status),
            "activity_score": att.activity_score,
        }
        for att, ev in attendance_rows
    ]

    att_total = len(attendance_data)
    att_attended = sum(1 for a in attendance_data if a["status"] in ("attended", "late"))
    att_rate = round(att_attended / att_total * 100, 1) if att_total > 0 else None

    # --- Homework ---
    hw_rows = (
        db.query(AssignmentSubmission, Assignment)
        .join(Assignment, Assignment.id == AssignmentSubmission.assignment_id)
        .filter(AssignmentSubmission.user_id == student_id)
        .order_by(AssignmentSubmission.submitted_at.desc())
        .limit(50)
        .all()
    )
    homework_data = [
        {
            "submission_id": sub.id,
            "assignment_id": sub.assignment_id,
            "assignment_title": asgn.title,
            "score": sub.score,
            "max_score": sub.max_score,
            "is_graded": sub.is_graded,
            "is_late": sub.is_late,
            "feedback": sub.feedback,
            "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
            "graded_at": sub.graded_at.isoformat() if sub.graded_at else None,
        }
        for sub, asgn in hw_rows
    ]

    hw_submitted = len(homework_data)
    graded = [h for h in homework_data if h["is_graded"] and h["score"] is not None]
    hw_avg_score = round(sum(h["score"] for h in graded) / len(graded), 1) if graded else None

    # --- LMS Progress ---
    # Course-level progress (lesson_id IS NULL, assignment_id IS NULL)
    course_rows = (
        db.query(StudentProgress)
        .options(joinedload(StudentProgress.course))
        .filter(
            StudentProgress.user_id == student_id,
            StudentProgress.lesson_id.is_(None),
            StudentProgress.assignment_id.is_(None),
        )
        .order_by(StudentProgress.last_accessed.desc())
        .all()
    )

    # Lesson-level progress (lesson_id IS NOT NULL)
    lesson_rows = (
        db.query(StudentProgress)
        .options(joinedload(StudentProgress.course), joinedload(StudentProgress.lesson))
        .filter(
            StudentProgress.user_id == student_id,
            StudentProgress.lesson_id.isnot(None),
        )
        .order_by(StudentProgress.last_accessed.desc())
        .all()
    )

    # Build per-course data from lesson-level rows
    lessons_by_course: dict = {}
    for sp in lesson_rows:
        cid = sp.course_id
        if cid not in lessons_by_course:
            lessons_by_course[cid] = []
        lessons_by_course[cid].append({
            "lesson_id": sp.lesson_id,
            "lesson_title": sp.lesson.title if sp.lesson else None,
            "status": sp.status,
            "completion_percentage": sp.completion_percentage,
            "last_accessed": sp.last_accessed.isoformat() if sp.last_accessed else None,
        })

    # Build course_progress from course-level rows (primary) or lesson-level (fallback)
    course_progress: dict = {}
    for sp in course_rows:
        cid = sp.course_id
        lessons = lessons_by_course.get(cid, [])
        completed_lessons = sum(1 for l in lessons if l["status"] == "completed") if lessons else 0
        course_progress[cid] = {
            "course_id": cid,
            "course_name": sp.course.title if sp.course else None,
            "status": sp.status,
            "completion_percentage": sp.completion_percentage,
            "total_lessons": len(lessons),
            "completed_lessons": completed_lessons,
            "avg_completion": sp.completion_percentage,
            "last_accessed": sp.last_accessed.isoformat() if sp.last_accessed else None,
            "lessons": lessons,
        }

    # Add any courses that only have lesson-level records
    for cid, lessons in lessons_by_course.items():
        if cid not in course_progress:
            avg_pct = round(sum(l["completion_percentage"] for l in lessons) / len(lessons), 1) if lessons else 0
            completed_lessons = sum(1 for l in lessons if l["status"] == "completed")
            course_progress[cid] = {
                "course_id": cid,
                "course_name": lessons[0].get("lesson_title", None),
                "status": "completed" if all(l["status"] == "completed" for l in lessons) else "in_progress",
                "completion_percentage": avg_pct,
                "total_lessons": len(lessons),
                "completed_lessons": completed_lessons,
                "avg_completion": avg_pct,
                "last_accessed": None,
                "lessons": lessons,
            }

    lms_data = list(course_progress.values())
    overall_lms = round(sum(c["avg_completion"] for c in lms_data) / len(lms_data), 1) if lms_data else None

    return {
        "student": {
            "id": student.id,
            "name": student.name,
            "email": student.email,
            "avatar_url": student.avatar_url,
            "created_at": student.created_at.isoformat() if student.created_at else None,
            "last_activity_date": student.last_activity_date.isoformat() if student.last_activity_date else None,
            "daily_streak": student.daily_streak,
            "assignment_zero_completed": student.assignment_zero_completed,
        },
        "groups": groups_data,
        "assignment_zero": az_data,
        "attendance": {
            "total": att_total,
            "attended": att_attended,
            "rate": att_rate,
            "records": attendance_data,
        },
        "homework": {
            "submitted": hw_submitted,
            "avg_score": hw_avg_score,
            "records": homework_data,
        },
        "lms_progress": {
            "overall": overall_lms,
            "courses": lms_data,
        },
    }
