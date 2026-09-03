"""SAT Checkpoints HTTP surface.

- GET  /checkpoints/me                     student: own checkpoints (auto-opens eligible ones)
- admin/staff endpoints under /checkpoints/admin — see Task 6 in the plan.
"""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.checkpoints import service
from src.checkpoints.models import StudentCheckpoint
from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import UserInDB

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
