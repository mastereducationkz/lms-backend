"""Parent-facing read endpoints (mounted at /parents).

A parent may only read data about a student they are linked to via the
`parent_students` table. Every child endpoint gates on `require_child`.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException
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
    """Teachers and curators the child studies with (from their groups + courses)."""
    require_child(student_id, current_user, db)
    role_by_id: dict = {}

    group_ids = [gs.group_id for gs in db.query(GroupStudent.group_id).filter(
        GroupStudent.student_id == student_id).all()]
    if group_ids:
        for g in db.query(Group).filter(Group.id.in_(group_ids)).all():
            if g.teacher_id and g.teacher_id not in role_by_id:
                role_by_id[g.teacher_id] = "teacher"
            if g.curator_id and g.curator_id not in role_by_id:
                role_by_id[g.curator_id] = "curator"

    # Course teachers (courses the student can access).
    from src.utils.course_access import get_user_courses
    for course in get_user_courses(student_id, db):
        if course.teacher_id and course.teacher_id not in role_by_id:
            role_by_id[course.teacher_id] = "teacher"

    contacts = []
    if role_by_id:
        for u in db.query(UserInDB).filter(UserInDB.id.in_(list(role_by_id.keys()))).all():
            contacts.append({"id": u.id, "name": u.name, "role": role_by_id.get(u.id)})
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
