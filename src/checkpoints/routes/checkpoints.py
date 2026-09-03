"""SAT Checkpoints HTTP surface.

- GET  /checkpoints/me                     student: own checkpoints (auto-opens eligible ones)
- admin/staff endpoints under /checkpoints/admin — see Task 6 in the plan.
"""
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.checkpoints import service
from src.checkpoints.completion import completed_lesson_ids
from src.checkpoints.models import CheckpointDefinition, CheckpointRequiredUnit, StudentCheckpoint
from src.checkpoints.schemas import DeadlineUpdate, DefinitionUpdate, GroupSettingsUpdate, OpenRequest
from src.config import get_db
from src.courses.models import Group, GroupStudent, Lesson, Step
from src.routes.auth import get_current_user_dependency
from src.schemas.models import UserInDB
from src.utils.permissions import check_group_access

checkpoints_router = APIRouter()
checkpoints_admin_router = APIRouter()


@checkpoints_router.get("/me")
def get_my_checkpoints(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    if current_user.role != "student":
        raise HTTPException(status_code=403, detail="Students only")
    groups = service.enabled_groups_for_student(db, current_user.id)
    if not groups:
        return {"enabled": False, "items": []}

    service.sync_student_checkpoints(db, current_user.id, commit=True)
    now = service.utcnow()
    items = []
    dirty = False
    for group in groups:
        for definition in service.definitions_for_group(db, group):
            row = service.get_row(db, current_user.id, group.id, definition.id)
            if row is not None and service.refresh_overdue([row], now):
                dirty = True
            items.append(service.serialize_for_student(db, current_user.id, group, definition, row))
    if dirty:
        db.commit()
    return {"enabled": True, "items": items}


# ======================================================================= admin / staff

READ_ROLES = ("admin", "head_curator", "head_teacher")


def _require_admin(user: UserInDB) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


def _require_group_read(user: UserInDB, group: Group, db: Session) -> None:
    if user.role in READ_ROLES:
        return
    if not check_group_access(group.id, user, db):
        raise HTTPException(status_code=403, detail="No access to this group")


def _group_or_404(db: Session, group_id: int) -> Group:
    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


def _definition_or_404(db: Session, checkpoint_id: int) -> CheckpointDefinition:
    d = db.get(CheckpointDefinition, checkpoint_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    return d


def _quiz_questions(db: Session, definition: CheckpointDefinition) -> List[Dict[str, Any]]:
    if definition.quiz_lesson_id is None:
        return []
    step = db.query(Step).filter(Step.lesson_id == definition.quiz_lesson_id,
                                 Step.content_type == "quiz").order_by(Step.order_index).first()
    if step is None or not step.content_text:
        return []
    try:
        return [q for q in (json.loads(step.content_text).get("questions") or []) if isinstance(q, dict)]
    except (ValueError, AttributeError):
        return []


def _serialize_definition(db: Session, d: CheckpointDefinition) -> Dict[str, Any]:
    titles = dict(db.query(Lesson.id, Lesson.title).filter(
        Lesson.id.in_([u.lesson_id for u in d.required_units] or [0])).all())
    return {
        "id": d.id, "course_id": d.course_id, "number": d.number, "title": d.title,
        "is_active": bool(d.is_active), "total_questions": d.total_questions,
        "quiz_lesson_id": d.quiz_lesson_id, "quiz": service.quiz_ref(db, d),
        "required_units": [{"lesson_id": u.lesson_id, "title": titles.get(u.lesson_id, ""), "kind": u.kind}
                           for u in d.required_units],
        "question_count": len(_quiz_questions(db, d)),
    }


@checkpoints_admin_router.get("/definitions")
def list_definitions(course_id: Optional[int] = Query(None),
                     current_user: UserInDB = Depends(get_current_user_dependency),
                     db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    if current_user.role not in READ_ROLES and current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Staff only")
    q = db.query(CheckpointDefinition)
    if course_id is not None:
        q = q.filter(CheckpointDefinition.course_id == course_id)
    return [_serialize_definition(db, d) for d in q.order_by(CheckpointDefinition.course_id, CheckpointDefinition.number).all()]


@checkpoints_admin_router.put("/definitions/{checkpoint_id}")
def update_definition(checkpoint_id: int, body: DefinitionUpdate,
                      current_user: UserInDB = Depends(get_current_user_dependency),
                      db: Session = Depends(get_db)) -> Dict[str, Any]:
    _require_admin(current_user)
    d = _definition_or_404(db, checkpoint_id)
    if body.title is not None:
        d.title = body.title
    if body.is_active is not None:
        d.is_active = body.is_active
    if body.total_questions is not None:
        d.total_questions = body.total_questions
    if body.quiz_lesson_id is not None:
        if db.get(Lesson, body.quiz_lesson_id) is None:
            raise HTTPException(status_code=404, detail="Quiz lesson not found")
        d.quiz_lesson_id = body.quiz_lesson_id
    if body.required_units is not None:
        kinds = sorted(u.kind for u in body.required_units)
        if kinds != ["math", "verbal", "verbal"]:
            raise HTTPException(status_code=400, detail="A checkpoint requires exactly 2 verbal units and 1 math unit")
        ids = [u.lesson_id for u in body.required_units]
        if len(set(ids)) != 3:
            raise HTTPException(status_code=400, detail="Required units must be distinct")
        found = {r[0] for r in db.query(Lesson.id).filter(Lesson.id.in_(ids)).all()}
        if found != set(ids):
            raise HTTPException(status_code=404, detail=f"Unknown lesson ids: {sorted(set(ids) - found)}")
        db.query(CheckpointRequiredUnit).filter(CheckpointRequiredUnit.checkpoint_id == d.id).delete(synchronize_session=False)
        db.flush()
        db.expire(d, ["required_units"])
        d.required_units = [CheckpointRequiredUnit(lesson_id=u.lesson_id, kind=u.kind, position=i)
                            for i, u in enumerate(body.required_units)]
    db.commit()
    db.refresh(d)
    return _serialize_definition(db, d)


@checkpoints_admin_router.get("/definitions/{checkpoint_id}/quiz-check")
def quiz_check(checkpoint_id: int, current_user: UserInDB = Depends(get_current_user_dependency),
               db: Session = Depends(get_db)) -> Dict[str, Any]:
    if current_user.role not in READ_ROLES and current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Staff only")
    d = _definition_or_404(db, checkpoint_id)
    questions = _quiz_questions(db, d)
    by = {"easy": 0, "medium": 0, "hard": 0, "unset": 0}
    for q in questions:
        key = str(q.get("difficulty") or "").lower()
        by[key if key in by else "unset"] += 1
    problems = []
    if d.quiz_lesson_id is None:
        problems.append("No quiz lesson linked")
    if len(questions) != d.total_questions:
        problems.append(f"Expected {d.total_questions} questions, found {len(questions)}")
    per_level = d.total_questions // 3
    for level in ("easy", "medium", "hard"):
        if by[level] != per_level:
            problems.append(f"Expected {per_level} {level} questions, found {by[level]}")
    if by["unset"]:
        problems.append(f"{by['unset']} questions have no difficulty set")
    return {"question_count": len(questions), "expected": d.total_questions, "by_difficulty": by, "problems": problems}


def _serialize_group(db: Session, g: Group) -> Dict[str, Any]:
    count = db.query(GroupStudent).filter(GroupStudent.group_id == g.id).count()
    return {"id": g.id, "name": g.name, "program_type": g.program_type,
            "teacher_name": g.teacher.name if g.teacher else None, "student_count": count,
            "checkpoints_enabled": bool(g.checkpoints_enabled),
            "checkpoints_start_number": g.checkpoints_start_number or 1}


@checkpoints_admin_router.get("/groups")
def list_groups(program_type: Optional[str] = Query("sat"),
                current_user: UserInDB = Depends(get_current_user_dependency),
                db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    q = db.query(Group).filter(Group.is_active == True)  # noqa: E712
    if program_type:
        q = q.filter(Group.program_type == program_type)
    groups = q.order_by(Group.name).all()
    if current_user.role not in READ_ROLES:
        groups = [g for g in groups if check_group_access(g.id, current_user, db)]
    return [_serialize_group(db, g) for g in groups]


@checkpoints_admin_router.patch("/groups/{group_id}")
def update_group_settings(group_id: int, body: GroupSettingsUpdate,
                          current_user: UserInDB = Depends(get_current_user_dependency),
                          db: Session = Depends(get_db)) -> Dict[str, Any]:
    _require_admin(current_user)
    group = _group_or_404(db, group_id)
    if body.start_number is not None:
        group.checkpoints_start_number = body.start_number
    if body.enabled is not None:
        group.checkpoints_enabled = body.enabled
    db.commit()
    opened = service.sync_group(db, group, commit=True) if group.checkpoints_enabled else 0
    return {"group_id": group.id, "checkpoints_enabled": bool(group.checkpoints_enabled),
            "checkpoints_start_number": group.checkpoints_start_number, "opened": opened}


@checkpoints_admin_router.get("/groups/{group_id}/matrix")
def group_matrix(group_id: int, current_user: UserInDB = Depends(get_current_user_dependency),
                 db: Session = Depends(get_db)) -> Dict[str, Any]:
    group = _group_or_404(db, group_id)
    _require_group_read(current_user, group, db)
    definitions = service.definitions_for_group(db, group, only_active=False)
    students = (db.query(UserInDB).join(GroupStudent, GroupStudent.student_id == UserInDB.id)
                .filter(GroupStudent.group_id == group.id).order_by(UserInDB.name).all())
    now = service.utcnow()
    dirty = False

    rows_by_key = {}
    if students and definitions:
        rows = db.query(StudentCheckpoint).filter(StudentCheckpoint.group_id == group.id).all()
        rows_by_key = {(r.student_id, r.checkpoint_id): r for r in rows}

    all_required = list(dict.fromkeys(u.lesson_id for d in definitions for u in d.required_units))
    titles = dict(db.query(Lesson.id, Lesson.title).filter(Lesson.id.in_(all_required or [0])).all())

    out_students = []
    for s in students:
        done = completed_lesson_ids(db, s.id, all_required)
        cells = []
        for d in definitions:
            row = rows_by_key.get((s.id, d.id))
            if row is not None and service.refresh_overdue([row], now):
                dirty = True
            units = [{"lesson_id": u.lesson_id, "title": titles.get(u.lesson_id, ""), "kind": u.kind,
                     "completed": u.lesson_id in done} for u in d.required_units]
            cell = service.serialize_row(row)
            cell.update({"checkpoint_id": d.id, "number": d.number, "units": units,
                         "locked_reason": service.locked_reason(units) if cell["status"] == "locked" else None})
            cells.append(cell)
        out_students.append({"student_id": s.id, "name": s.name, "email": s.email, "cells": cells})
    if dirty:
        db.commit()
    return {"group": _serialize_group(db, group),
            "definitions": [_serialize_definition(db, d) for d in definitions],
            "students": out_students}


def _rows_payload(rows) -> Dict[str, Any]:
    return {"changed": len(rows), "rows": [dict(service.serialize_row(r), student_id=r.student_id) for r in rows]}


@checkpoints_admin_router.post("/groups/{group_id}/checkpoints/{checkpoint_id}/open")
def open_checkpoint(group_id: int, checkpoint_id: int, body: OpenRequest,
                    current_user: UserInDB = Depends(get_current_user_dependency),
                    db: Session = Depends(get_db)) -> Dict[str, Any]:
    _require_admin(current_user)
    group = _group_or_404(db, group_id)
    d = _definition_or_404(db, checkpoint_id)
    rows = service.open_for_students(db, group=group, definition=d, student_ids=body.student_ids,
                                     deadline=body.deadline, actor_id=current_user.id)
    return _rows_payload(rows)


@checkpoints_admin_router.post("/groups/{group_id}/checkpoints/{checkpoint_id}/reopen")
def reopen_checkpoint(group_id: int, checkpoint_id: int, body: OpenRequest,
                      current_user: UserInDB = Depends(get_current_user_dependency),
                      db: Session = Depends(get_db)) -> Dict[str, Any]:
    _require_admin(current_user)
    group = _group_or_404(db, group_id)
    d = _definition_or_404(db, checkpoint_id)
    rows = service.reopen_for_students(db, group=group, definition=d, student_ids=body.student_ids,
                                       deadline=body.deadline, actor_id=current_user.id)
    return _rows_payload(rows)


@checkpoints_admin_router.patch("/student-checkpoints/{row_id}")
def update_deadline(row_id: int, body: DeadlineUpdate,
                    current_user: UserInDB = Depends(get_current_user_dependency),
                    db: Session = Depends(get_db)) -> Dict[str, Any]:
    _require_admin(current_user)
    row = db.get(StudentCheckpoint, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Student checkpoint not found")
    service.set_deadline(db, row, body.deadline, current_user.id)
    return service.serialize_row(row)


@checkpoints_admin_router.get("/groups/{group_id}/checkpoints/{checkpoint_id}/results")
def checkpoint_results(group_id: int, checkpoint_id: int,
                       current_user: UserInDB = Depends(get_current_user_dependency),
                       db: Session = Depends(get_db)) -> List[Dict[str, Any]]:
    group = _group_or_404(db, group_id)
    _require_group_read(current_user, group, db)
    d = _definition_or_404(db, checkpoint_id)
    students = (db.query(UserInDB).join(GroupStudent, GroupStudent.student_id == UserInDB.id)
                .filter(GroupStudent.group_id == group.id).order_by(UserInDB.name).all())
    out = []
    for s in students:
        row = service.get_row(db, s.id, group.id, d.id)
        out.append(dict(service.serialize_row(row), student_id=s.id, name=s.name, email=s.email))
    return out
