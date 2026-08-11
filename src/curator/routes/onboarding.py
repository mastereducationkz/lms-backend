"""Curator onboarding kanban API."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import datetime, timezone
from typing import Optional

from src.config import get_db
from src.schemas.models import (
    CuratorOnboarding, UserInDB, Group, AssignmentZeroSubmission,
)
from src.curator.schemas import OnboardingStatusUpdate
from src.curator.onboarding_service import telegram_link
from src.routes.auth import get_current_user_dependency

router = APIRouter()

VISIBLE_STATUSES = ("new", "in_progress", "done")
SETTABLE_STATUSES = ("new", "in_progress", "done")
ALLOWED_ROLES = ("curator", "head_curator", "admin")


def _require_role(current_user: UserInDB) -> None:
    if current_user.role not in ALLOWED_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied. Required roles: {', '.join(ALLOWED_ROLES)}",
        )


def _serialize(row: CuratorOnboarding, az: Optional[AssignmentZeroSubmission]) -> dict:
    student = row.student
    return {
        "id": row.id,
        "student_id": row.student_id,
        "student_name": (student.official_full_name or student.name) if student else "",
        "group_id": row.group_id,
        "group_name": row.group.name if row.group else None,
        "curator_id": row.curator_id,
        "curator_name": (row.curator.name if row.curator else None),
        "telegram_id": az.telegram_id if az else None,
        "telegram_link": telegram_link(az.telegram_id) if az else None,
        "phone_number": az.phone_number if az else None,
        "parent_phone_number": az.parent_phone_number if az else None,
        "status": row.status,
        "created_at": row.created_at,
    }


@router.get("/")
def list_onboarding(
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
    curator_id: Optional[int] = Query(None),
):
    _require_role(current_user)
    q = db.query(CuratorOnboarding).filter(
        CuratorOnboarding.status.in_(VISIBLE_STATUSES))
    if current_user.role == "curator":
        q = q.filter(CuratorOnboarding.curator_id == current_user.id)
    elif curator_id is not None:
        q = q.filter(CuratorOnboarding.curator_id == curator_id)
    rows = q.order_by(CuratorOnboarding.created_at.desc()).all()

    # batch-load contact info
    student_ids = [r.student_id for r in rows]
    az_map = {}
    if student_ids:
        for az in db.query(AssignmentZeroSubmission).filter(
                AssignmentZeroSubmission.user_id.in_(student_ids)).all():
            az_map[az.user_id] = az
    return {"cards": [_serialize(r, az_map.get(r.student_id)) for r in rows]}


@router.patch("/{card_id}")
def update_onboarding(
    card_id: int,
    payload: OnboardingStatusUpdate,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    _require_role(current_user)
    if payload.status not in SETTABLE_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    row = db.query(CuratorOnboarding).filter(CuratorOnboarding.id == card_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    if current_user.role == "curator" and row.curator_id != current_user.id:
        raise HTTPException(status_code=403, detail="forbidden")

    row.status = payload.status
    if payload.status == "done":
        row.completed_at = datetime.now(timezone.utc)
        row.completed_by = current_user.id
    else:
        row.completed_at = None
        row.completed_by = None
    db.commit()
    db.refresh(row)
    az = db.query(AssignmentZeroSubmission).filter(
        AssignmentZeroSubmission.user_id == row.student_id).first()
    return _serialize(row, az)
