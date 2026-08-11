"""Curator onboarding kanban API (legacy LMS UI).

The CRM workspace is the canonical onboarding interface; this router stays for backward
compatibility with old bookmarks and the mobile clients, and **delegates every rule** to
:mod:`src.curator.onboarding_core` rather than keeping a second copy of them. There is no
dual-write: both surfaces mutate the same rows through the same service.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.config import get_db
from src.curator.onboarding_core import (
    BOARD_STATUSES,
    SETTABLE_STATUSES,
    OnboardingActor,
    OnboardingNotFound,
    OnboardingPermissionError,
    get_card,
    load_board,
    serialize_card,
    set_status,
    telegram_link,
)
from src.curator.schemas import OnboardingStatusUpdate
from src.routes.auth import get_current_user_dependency
from src.schemas.models import AssignmentZeroSubmission, UserInDB

router = APIRouter()

VISIBLE_STATUSES = BOARD_STATUSES
ALLOWED_ROLES = ("curator", "head_curator", "admin")


def _require_role(current_user: UserInDB) -> None:
    if current_user.role not in ALLOWED_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied. Required roles: {', '.join(ALLOWED_ROLES)}",
        )


def _serialize(row, az: Optional[AssignmentZeroSubmission]) -> dict:
    """Legacy card shape — kept field-for-field so existing clients do not break, with the
    new lifecycle fields added alongside."""
    data = serialize_card(row)
    data.update(
        {
            "telegram_id": az.telegram_id if az else None,
            "telegram_link": telegram_link(az.telegram_id) if az else None,
            "phone_number": az.phone_number if az else None,
            "parent_phone_number": az.parent_phone_number if az else None,
        }
    )
    return data


@router.get("/")
def list_onboarding(
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
    curator_id: Optional[int] = Query(None),
):
    _require_role(current_user)
    if current_user.role == "curator":
        curator_ids = [current_user.id]
    elif curator_id is not None:
        curator_ids = [curator_id]
    else:
        curator_ids = None

    rows = load_board(db, curator_ids=curator_ids)

    student_ids = [r.student_id for r in rows]
    az_map = {}
    if student_ids:
        for az in (
            db.query(AssignmentZeroSubmission)
            .filter(AssignmentZeroSubmission.user_id.in_(student_ids))
            .all()
        ):
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
    actor = OnboardingActor.from_user(current_user)
    try:
        row = get_card(db, card_id, actor)
        row = set_status(db, row, payload.status, actor)
    except OnboardingNotFound:
        raise HTTPException(status_code=404, detail="not found") from None
    except OnboardingPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    az = (
        db.query(AssignmentZeroSubmission)
        .filter(AssignmentZeroSubmission.user_id == row.student_id)
        .first()
    )
    return _serialize(row, az)
