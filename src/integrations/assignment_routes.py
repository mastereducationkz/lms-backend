"""HTTP surface of the platform-test assignments (Platform Integration Pack §6.3, E1/E2),
mounted under ``/integrations``. Everything answers 503 while ``PLATFORM_ASSIGNMENTS_ENABLED``
is off so the clients fall back to their pre-existing behaviour.

- ``GET  /weekly-tests/me``                          the student's open platform tests (countdown card)
- ``GET  /assignments/{id}/platform-progress``      student: own checkmarks; staff: the group matrix
- ``PATCH /groups/{id}/platform-tests``              per-group opt-out (admin, head curator, group staff)
- ``POST /platform-tests/sync``                      admin: (re)create for the current sets now
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import get_db
from src.integrations import platform_assignments, platform_progress
from src.integrations.models import PlatformTestAssignment
from src.routes.auth import get_current_user_dependency

platform_assignments_router = APIRouter()


class OptOutRequest(BaseModel):
    opt_out: bool


def _require_enabled() -> None:
    if not platform_assignments.enabled():
        raise HTTPException(status_code=503, detail="Platform-test assignments are disabled")


def _role(user) -> str:
    return (getattr(user, "role", "") or "").strip().lower()


def _manages_group(db: Session, user, group) -> bool:
    """Who may see a group's matrix and flip its opt-out: admins and head curators for any
    group, the group's own curator/teacher, and head teachers with access to the group."""
    role = _role(user)
    if role in ("admin", "head_curator"):
        return True
    if role == "curator":
        return group.curator_id == user.id
    if role == "teacher":
        return group.teacher_id == user.id
    if role == "head_teacher":
        try:
            from src.gamification.routes.leaderboard import head_teacher_can_access_group

            return bool(head_teacher_can_access_group(db, user.id, group.id))
        except Exception:  # noqa: BLE001 - helper unavailable: no access rather than a 500
            return False
    return False


def _platform_assignment_or_404(db: Session, assignment_id: int):
    from src.assignments.models import Assignment

    assignment = db.get(Assignment, assignment_id)
    link = (
        db.query(PlatformTestAssignment).filter(PlatformTestAssignment.assignment_id == assignment_id).first()
        if assignment is not None else None
    )
    if assignment is None or link is None or assignment.assignment_type != platform_assignments.ASSIGNMENT_TYPE:
        raise HTTPException(status_code=404, detail="Platform-test assignment not found")
    return assignment


@platform_assignments_router.get("/weekly-tests/me")
def weekly_tests_me(user=Depends(get_current_user_dependency), db: Session = Depends(get_db)) -> dict:
    _require_enabled()
    if _role(user) != "student":
        return {"items": []}
    return {"items": platform_progress.weekly_tests_for_student(db, user.id)}


@platform_assignments_router.get("/assignments/{assignment_id}/platform-progress")
def platform_progress_view(
    assignment_id: int, user=Depends(get_current_user_dependency), db: Session = Depends(get_db)
) -> dict:
    _require_enabled()
    assignment = _platform_assignment_or_404(db, assignment_id)
    from src.courses.models import Group, GroupStudent

    group = db.get(Group, assignment.group_id) if assignment.group_id else None
    if _role(user) == "student":
        member = (
            db.query(GroupStudent)
            .filter(GroupStudent.group_id == assignment.group_id, GroupStudent.student_id == user.id)
            .first()
        )
        if member is None:
            raise HTTPException(status_code=403, detail="Not a member of this group")
        return platform_progress.student_progress(db, assignment, user.id)
    if group is None or not _manages_group(db, user, group):
        raise HTTPException(status_code=403, detail="No access to this group")
    return {
        "assignment": platform_progress.assignment_summary(assignment),
        "students": platform_progress.group_matrix(db, assignment),
    }


@platform_assignments_router.patch("/groups/{group_id}/platform-tests")
def set_group_platform_tests(
    group_id: int, body: OptOutRequest, user=Depends(get_current_user_dependency), db: Session = Depends(get_db)
) -> dict:
    _require_enabled()
    from src.courses.models import Group

    group = db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    if not _manages_group(db, user, group):
        raise HTTPException(status_code=403, detail="No access to this group")
    sync = platform_assignments.set_group_opt_out(db, group, body.opt_out)
    return {"group_id": group.id, "opt_out": bool(group.platform_tests_opt_out), "sync": sync}


@platform_assignments_router.post("/platform-tests/sync")
def sync_platform_tests(
    include_past: bool = False, user=Depends(get_current_user_dependency), db: Session = Depends(get_db)
) -> dict:
    _require_enabled()
    if _role(user) != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    from src.integrations import platform_calendar

    out = platform_assignments.sync_all_active(db, include_past=include_past)
    out["calendar"] = platform_calendar.sync_all_active(db)
    return out
