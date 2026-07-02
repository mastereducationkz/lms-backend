"""Internal CRM ↔ LMS routes (service-key auth)."""
import os
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from src.config import get_db
from src.schemas.models import (
    UserInDB,
    LessonRequest,
    LessonRequestSchema,
    CreateLessonRequestSchema,
)
from src.lesson_requests.services import create_lesson_request_record
from src.lesson_requests.helpers import enrich_request, notify_approvers_of_request

router = APIRouter()


def _require_crm_internal_key(
    x_crm_service_key: Annotated[str | None, Header(alias="X-CRM-Service-Key")] = None,
) -> None:
    expected = os.getenv("CRM_INTERNAL_SERVICE_KEY", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="LMS: set CRM_INTERNAL_SERVICE_KEY to enable /internal/crm routes",
        )
    if not x_crm_service_key or x_crm_service_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-CRM-Service-Key")


def _require_lms_teacher_id(
    x_lms_teacher_id: Annotated[str | None, Header(alias="X-Lms-Teacher-Id")] = None,
) -> int:
    if not x_lms_teacher_id:
        raise HTTPException(status_code=400, detail="Missing X-Lms-Teacher-Id header")
    try:
        return int(x_lms_teacher_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid X-Lms-Teacher-Id")


@router.get("/health", dependencies=[Depends(_require_crm_internal_key)])
async def crm_internal_health() -> dict[str, str]:
    return {"status": "ok", "service": "lms_crm_internal"}


@router.post(
    "/lesson-requests",
    response_model=LessonRequestSchema,
    dependencies=[Depends(_require_crm_internal_key)],
)
async def crm_create_lesson_request(
    data: CreateLessonRequestSchema,
    teacher_id: int = Depends(_require_lms_teacher_id),
    db: Session = Depends(get_db),
):
    """Create a lesson request on behalf of a CRM teacher."""
    requester = (
        db.query(UserInDB)
        .filter(
            UserInDB.id == teacher_id,
            UserInDB.role.in_(("teacher", "head_teacher", "admin")),
            UserInDB.is_active == True,
        )
        .first()
    )
    if not requester:
        raise HTTPException(status_code=404, detail="Teacher not found")

    new_request = create_lesson_request_record(db, requester, data)
    notify_approvers_of_request(db, new_request, requester)
    return enrich_request(new_request, db)


@router.get(
    "/lesson-requests/me",
    response_model=List[LessonRequestSchema],
    dependencies=[Depends(_require_crm_internal_key)],
)
async def crm_get_my_lesson_requests(
    teacher_id: int = Depends(_require_lms_teacher_id),
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """List lesson requests for a CRM teacher."""
    query = db.query(LessonRequest).filter(LessonRequest.requester_id == teacher_id)
    if status_filter:
        query = query.filter(LessonRequest.status == status_filter)
    requests = query.order_by(LessonRequest.created_at.desc()).all()
    return [enrich_request(r, db) for r in requests]


@router.get(
    "/lesson-requests/teachers/available",
    dependencies=[Depends(_require_crm_internal_key)],
)
async def crm_get_available_teachers(
    datetime_str: str = Query(..., description="ISO datetime for the lesson slot"),
    group_id: Optional[int] = None,
    teacher_id: int = Depends(_require_lms_teacher_id),
    db: Session = Depends(get_db),
):
    """Proxy available teachers lookup for CRM substitution UI."""
    from datetime import timedelta
    from src.schemas.models import Group, LessonSchedule, Event, Course, CourseGroupAccess

    requester = db.query(UserInDB).filter(UserInDB.id == teacher_id).first()
    if not requester:
        raise HTTPException(status_code=404, detail="Teacher not found")

    try:
        from datetime import datetime
        target_dt = datetime.fromisoformat(datetime_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid datetime format")

    window_start = target_dt - timedelta(minutes=30)
    window_end = target_dt + timedelta(minutes=90)

    query = db.query(UserInDB).filter(
        UserInDB.role == "teacher",
        UserInDB.is_active == True,
        UserInDB.no_substitutions == False,
        UserInDB.id != teacher_id,
    )

    if group_id:
        course_ids = [
            c[0]
            for c in db.query(CourseGroupAccess.course_id).filter(
                CourseGroupAccess.group_id == group_id,
                CourseGroupAccess.is_active == True,
            ).all()
        ]
        if course_ids:
            relevant_group_ids = [
                g[0]
                for g in db.query(CourseGroupAccess.group_id).filter(
                    CourseGroupAccess.course_id.in_(course_ids),
                    CourseGroupAccess.is_active == True,
                ).all()
            ]
            allowed_teacher_ids = set()
            if relevant_group_ids:
                for t in db.query(Group.teacher_id).filter(Group.id.in_(relevant_group_ids)).all():
                    allowed_teacher_ids.add(t[0])
            for ct in db.query(Course.teacher_id).filter(Course.id.in_(course_ids)).all():
                if ct[0]:
                    allowed_teacher_ids.add(ct[0])
            if allowed_teacher_ids:
                query = query.filter(UserInDB.id.in_(allowed_teacher_ids))
            else:
                query = query.filter(UserInDB.id == -1)

    teachers = query.all()
    busy_teacher_ids = set()
    for ev in db.query(Event).filter(
        Event.is_active == True,
        Event.teacher_id.isnot(None),
        Event.start_datetime < window_end,
        Event.end_datetime > window_start,
    ).all():
        busy_teacher_ids.add(ev.teacher_id)

    available = [
        {"id": t.id, "name": t.name, "email": t.email}
        for t in teachers
        if t.id not in busy_teacher_ids
    ]
    return {"available_teachers": available}
