"""``/targets`` — structured per-track targets and progress (Platform Integration Pack §6.4, E5).

- ``GET  /me``                      student: tracks, targets, progress (the tile payload)
- ``PUT  /me/{track}``              student sets their own target (source "student")
- ``GET  /students/{id}``           group curator/teacher, head curator, head teacher with access,
                                    admin, or the student's parent (read-only)
- ``PUT  /students/{id}/{track}``   staff override (source "staff", set_by kept visible)
All answer 503 while ``PLATFORM_TARGETS_ENABLED`` is off.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import get_db
from src.integrations import targets as tg
from src.integrations import targets_progress as tp
from src.routes.auth import get_current_user_dependency

targets_router = APIRouter()


class TargetsBody(BaseModel):
    targets: dict[str, Any]


def _require_enabled() -> None:
    if not tg.enabled():
        raise HTTPException(status_code=503, detail="Targets are disabled")


def _role(user) -> str:
    return (getattr(user, "role", "") or "").strip().lower()


def _student_or_404(db: Session, student_id: int):
    from src.auth.models import UserInDB

    student = db.get(UserInDB, student_id)
    if student is None or _role(student) != "student":
        raise HTTPException(status_code=404, detail="Student not found")
    return student


def _student_groups(db: Session, student_id: int) -> list:
    from src.courses.models import Group, GroupStudent

    return (
        db.query(Group)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(GroupStudent.student_id == student_id)
        .all()
    )


def _is_parent_of(db: Session, user, student_id: int) -> bool:
    from src.parents.models import ParentStudent

    return (
        db.query(ParentStudent)
        .filter(ParentStudent.parent_id == user.id, ParentStudent.student_id == student_id)
        .first()
        is not None
    )


def _manages_student(db: Session, user, student_id: int) -> bool:
    role = _role(user)
    if role in ("admin", "head_curator"):
        return True
    groups = _student_groups(db, student_id)
    if role == "curator":
        return any(g.curator_id == user.id for g in groups)
    if role == "teacher":
        return any(g.teacher_id == user.id for g in groups)
    if role == "head_teacher":
        try:
            from src.gamification.routes.leaderboard import head_teacher_can_access_group

            return any(head_teacher_can_access_group(db, user.id, g.id) for g in groups)
        except Exception:  # noqa: BLE001 - helper unavailable: no access rather than a 500
            return False
    return False


def _can_read(db: Session, user, student_id: int) -> bool:
    if user.id == student_id:
        return True
    if _role(user) == "parent":
        return _is_parent_of(db, user, student_id)
    return _manages_student(db, user, student_id)


def _set(db: Session, student_id: int, track: str, body: TargetsBody, *, source: str, set_by: int) -> dict:
    try:
        return tg.set_target(db, student_id, track, body.targets, source=source, set_by=set_by)
    except tg.TargetsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@targets_router.get("/me")
def my_targets(user=Depends(get_current_user_dependency), db: Session = Depends(get_db)) -> dict:
    _require_enabled()
    if _role(user) != "student":
        raise HTTPException(status_code=403, detail="Students only")
    return tp.student_progress(db, user, tg.get_targets(db, user.id))


@targets_router.put("/me/{track}")
def set_my_target(track: str, body: TargetsBody, user=Depends(get_current_user_dependency),
                  db: Session = Depends(get_db)) -> dict:
    _require_enabled()
    if _role(user) != "student":
        raise HTTPException(status_code=403, detail="Students only")
    return _set(db, user.id, track, body, source="student", set_by=user.id)


@targets_router.get("/students/{student_id}")
def student_targets(student_id: int, user=Depends(get_current_user_dependency),
                    db: Session = Depends(get_db)) -> dict:
    _require_enabled()
    student = _student_or_404(db, student_id)
    if not _can_read(db, user, student.id):
        raise HTTPException(status_code=403, detail="No access to this student")
    return tp.student_progress(db, student, tg.get_targets(db, student.id))


@targets_router.put("/students/{student_id}/{track}")
def set_student_target(student_id: int, track: str, body: TargetsBody,
                       user=Depends(get_current_user_dependency), db: Session = Depends(get_db)) -> dict:
    _require_enabled()
    student = _student_or_404(db, student_id)
    if not _manages_student(db, user, student.id):
        raise HTTPException(status_code=403, detail="No access to this student")
    return _set(db, student.id, track, body, source="staff", set_by=user.id)
