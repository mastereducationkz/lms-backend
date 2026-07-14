"""Parent-facing read endpoints (mounted at /parents).

A parent may only read data about a student they are linked to via the
`parent_students` table. Every child endpoint gates on `require_child`.
"""
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import (
    UserInDB, ParentStudent, Group, GroupStudent, AssignmentSubmission,
)

router = APIRouter()


def _group_name_for_student(db: Session, student_id: int):
    gs = db.query(GroupStudent).filter(GroupStudent.student_id == student_id).first()
    if not gs:
        return None
    g = db.query(Group).filter(Group.id == gs.group_id).first()
    return g.name if g else None


def require_child(student_id: int, current_user: UserInDB, db: Session) -> UserInDB:
    """Ensure the caller is a parent linked to `student_id`; return the child user."""
    if current_user.role != "parent":
        raise HTTPException(status_code=403, detail="Only parents can access this endpoint")
    link = db.query(ParentStudent).filter(
        ParentStudent.parent_id == current_user.id,
        ParentStudent.student_id == student_id,
    ).first()
    if not link:
        raise HTTPException(status_code=403, detail="Not your child")
    student = db.query(UserInDB).filter(UserInDB.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    return student


@router.get("/me/children")
def my_children(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """The children linked to the current parent. `id` is the student's user id."""
    if current_user.role != "parent":
        raise HTTPException(status_code=403, detail="Only parents can access this endpoint")
    links = db.query(ParentStudent).filter(ParentStudent.parent_id == current_user.id).all()
    children = []
    for link in links:
        student = db.query(UserInDB).filter(UserInDB.id == link.student_id).first()
        if not student:
            continue
        children.append({
            "id": student.id,
            "name": student.name,
            "email": student.email,
            "group_name": _group_name_for_student(db, student.id),
        })
    return children


@router.get("/children/{student_id}")
def child_profile(
    student_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    child = require_child(student_id, current_user, db)
    return {
        "id": child.id,
        "name": child.name,
        "email": child.email,
        "student_id": child.student_id,
        "group_name": _group_name_for_student(db, child.id),
    }


@router.get("/children/{student_id}/progress")
def child_progress(
    student_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    require_child(student_id, current_user, db)
    from src.progress.routes.progress import build_student_progress_overview
    return build_student_progress_overview(db, student_id)


@router.get("/children/{student_id}/contacts")
def child_contacts(
    student_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Teachers and curators the child studies with, each tagged with the course/group
    it relates to so the parent sees who teaches what. Curators keep the curator role even
    if they also teach a course. Shape: [{id, name, role, subjects: [str]}]."""
    require_child(student_id, current_user, db)
    # id -> {"role": str, "subjects": set[str]}
    info_by_id: dict = {}

    def _add(uid, role, subject):
        if not uid:
            return
        entry = info_by_id.setdefault(uid, {"role": role, "subjects": set()})
        # curator is the more "sticky" role — never downgrade it to teacher.
        if role == "curator":
            entry["role"] = "curator"
        if subject:
            entry["subjects"].add(subject)

    group_ids = [gs.group_id for gs in db.query(GroupStudent.group_id).filter(
        GroupStudent.student_id == student_id).all()]
    if group_ids:
        for g in db.query(Group).filter(Group.id.in_(group_ids)).all():
            _add(g.teacher_id, "teacher", g.name)
            _add(g.curator_id, "curator", g.name)

    # Course teachers (courses the student can access).
    from src.utils.course_access import get_user_courses
    for course in get_user_courses(student_id, db):
        _add(course.teacher_id, "teacher", course.title)

    contacts = []
    if info_by_id:
        users = {u.id: u for u in db.query(UserInDB).filter(
            UserInDB.id.in_(list(info_by_id.keys()))).all()}
        for uid, info in info_by_id.items():
            u = users.get(uid)
            if not u:
                continue
            contacts.append({
                "id": u.id,
                "name": u.name,
                "role": info["role"],
                "subjects": sorted(info["subjects"]),
            })
    # Teachers first, then curators; alphabetical within each.
    contacts.sort(key=lambda c: (0 if c["role"] == "teacher" else 1, c["name"]))
    return contacts


@router.get("/children/{student_id}/assignments")
def child_assignments(
    student_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    require_child(student_id, current_user, db)
    from src.assignments.routes.assignments import (
        _student_assignments, assignment_visible_to_student, _build_student_assignment_status,
    )
    assignments = [
        a for a in _student_assignments(db, student_id).all()
        if assignment_visible_to_student(student_id, a, db)
    ]
    result = []
    for a in assignments:
        submission = db.query(AssignmentSubmission).filter(
            AssignmentSubmission.assignment_id == a.id,
            AssignmentSubmission.user_id == student_id,
        ).first()
        status = _build_student_assignment_status(submission, a)
        result.append({
            "id": a.id,
            "title": a.title,
            "due_date": a.due_date,
            "max_score": a.max_score,
            **status,
        })
    return result


@router.get("/children/{student_id}/attendance")
def child_attendance(
    student_id: int,
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """A child's class lessons for a month with attendance status (для мини-календаря
    и журнала). status: attended | late | missed | registered | cancelled | null."""
    require_child(student_id, current_user, db)
    from src.schemas.models import Event, EventGroup, Attendance
    from src.services.attendance_service import attendance_status_to_ui

    now = datetime.now()
    y = year or now.year
    m = month or now.month
    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)

    group_ids = [gs.group_id for gs in db.query(GroupStudent.group_id).filter(
        GroupStudent.student_id == student_id).all()]
    if not group_ids:
        return []

    events = db.query(Event).join(EventGroup, EventGroup.event_id == Event.id).filter(
        EventGroup.group_id.in_(group_ids),
        Event.event_type == "class",
        Event.is_active == True,
        Event.start_datetime >= start,
        Event.start_datetime < end,
    ).distinct().all()
    if not events:
        return []

    event_ids = [e.id for e in events]
    att_map = {
        a.event_id: a
        for a in db.query(Attendance).filter(
            Attendance.user_id == student_id,
            Attendance.event_id.in_(event_ids),
        ).all()
    }

    result = []
    for e in events:
        a = att_map.get(e.id)
        result.append({
            "event_id": e.id,
            "title": e.title,
            "topic": getattr(e, "topic", None),
            "start_datetime": e.start_datetime,
            "status": attendance_status_to_ui(a.status) if a else None,
            "activity_score": a.activity_score if a else None,
        })
    result.sort(key=lambda x: x["start_datetime"] or start)
    return result
