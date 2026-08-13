"""
Lesson Substitution, Reschedule & Cancel Request Routes
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import json
import logging

from src.config import get_db
from src.schemas.models import (
    UserInDB, Group, LessonSchedule, Event,
    LessonRequest, LessonRequestSchema, CreateLessonRequestSchema, ResolveLessonRequestSchema,
    Course, CourseGroupAccess,
)
from src.routes.auth import get_current_user_dependency
from src.services.event_service import EventService
from src.lesson_requests.services import (
    create_lesson_request_record,
    get_group_ids_in_head_teacher_scope,
    user_can_resolve_request,
    requester_self_approves,
)
from src.lesson_requests.helpers import (
    enrich_request,
    enrich_requests,
    apply_approved_request,
    notify_approvers_of_request,
    notify_approvers_of_confirmation,
    notify_resolution,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_admin_or_head_teacher(current_user: UserInDB = Depends(get_current_user_dependency)) -> UserInDB:
    if current_user.role not in ("admin", "head_teacher"):
        raise HTTPException(status_code=403, detail="Admin or head teacher access required")
    return current_user


# =============================================================================
# TEACHER ENDPOINTS
# =============================================================================

@router.post("/", response_model=LessonRequestSchema)
async def create_lesson_request(
    data: CreateLessonRequestSchema,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Create a new lesson substitution, reschedule, or cancel request (teacher only).

    A teacher who also HEADS the group's subject changes their OWN lessons without
    approval: the request is applied and recorded as approved immediately, so it still
    shows up in the Lesson Requests list. Everyone else still routes to a head teacher.
    """
    if current_user.role not in ("teacher", "admin"):
        raise HTTPException(status_code=403, detail="Only teachers can create lesson requests")

    group = db.query(Group).filter(Group.id == data.group_id).first()
    self_approve = requester_self_approves(db, current_user.id, group)

    new_request = create_lesson_request_record(db, current_user, data, skip_limits=self_approve)

    if self_approve:
        apply_approved_request(db, new_request, current_user.id)
        new_request.status = "approved"
        new_request.resolved_by = current_user.id
        new_request.resolved_at = datetime.now(timezone.utc)
        new_request.admin_comment = "Автоматически применено: педагог ведёт этот предмет"
        db.commit()
        db.refresh(new_request)
        notify_resolution(db, new_request, approved=True)
    else:
        notify_approvers_of_request(db, new_request, current_user)
    return enrich_request(new_request, db)


@router.get("/me", response_model=List[LessonRequestSchema])
async def get_my_lesson_requests(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Get lesson requests created by the current teacher."""
    if current_user.role not in ("teacher", "admin"):
        raise HTTPException(status_code=403, detail="Only teachers can view their requests")

    query = db.query(LessonRequest).filter(LessonRequest.requester_id == current_user.id)
    if status_filter:
        query = query.filter(LessonRequest.status == status_filter)

    requests = query.order_by(LessonRequest.created_at.desc()).all()
    return enrich_requests(requests, db)


@router.get("/incoming", response_model=List[LessonRequestSchema])
async def get_incoming_requests(
    history: bool = False,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Get requests where current user is a candidate substitute."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can view incoming requests")

    all_pending = db.query(LessonRequest).filter(
        LessonRequest.status == "pending_teacher",
        LessonRequest.request_type == "substitution",
    ).all()

    results = []
    for req in all_pending:
        if req.substitute_teacher_ids:
            try:
                ids = json.loads(req.substitute_teacher_ids)
                if current_user.id in ids:
                    results.append(req)
            except (json.JSONDecodeError, TypeError):
                pass

    if history:
        confirmed_by_me = db.query(LessonRequest).filter(
            LessonRequest.confirmed_teacher_id == current_user.id
        ).all()
        existing_ids = {r.id for r in results}
        for r in confirmed_by_me:
            if r.id not in existing_ids:
                results.append(r)

    results.sort(key=lambda x: x.created_at, reverse=True)
    return enrich_requests(results, db)


@router.post("/{request_id}/confirm", response_model=LessonRequestSchema)
async def confirm_substitution_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Substitute teacher confirms they can take the class."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can confirm requests")

    req = db.query(LessonRequest).filter(LessonRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    if req.status != "pending_teacher":
        raise HTTPException(status_code=400, detail="Request is not pending teacher confirmation")

    candidate_ids = []
    if req.substitute_teacher_ids:
        try:
            candidate_ids = json.loads(req.substitute_teacher_ids)
        except (json.JSONDecodeError, TypeError):
            pass

    if current_user.id not in candidate_ids:
        raise HTTPException(status_code=403, detail="You are not a candidate for this request")

    req.status = "pending"
    req.confirmed_teacher_id = current_user.id
    req.substitute_teacher_id = current_user.id
    db.commit()
    db.refresh(req)

    notify_approvers_of_confirmation(db, req, current_user)
    return enrich_request(req, db)


@router.post("/{request_id}/decline", response_model=LessonRequestSchema)
async def decline_substitution_request(
    request_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Substitute teacher declines the request."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can decline requests")

    req = db.query(LessonRequest).filter(LessonRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    candidate_ids = []
    if req.substitute_teacher_ids:
        try:
            candidate_ids = json.loads(req.substitute_teacher_ids)
        except (json.JSONDecodeError, TypeError):
            pass

    if current_user.id not in candidate_ids:
        raise HTTPException(status_code=403, detail="You are not a candidate for this request")

    new_ids = [uid for uid in candidate_ids if uid != current_user.id]
    req.substitute_teacher_ids = json.dumps(new_ids)

    if not new_ids:
        req.status = "rejected"
        req.admin_comment = "All candidates declined."
        req.resolved_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(req)
    return enrich_request(req, db)


# =============================================================================
# ADMIN / HEAD TEACHER ENDPOINTS
# =============================================================================

@router.get("/", response_model=List[LessonRequestSchema])
async def list_lesson_requests(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(_require_admin_or_head_teacher),
):
    """List lesson requests. Admin sees all; head teacher sees scoped groups."""
    query = db.query(LessonRequest)

    if current_user.role == "head_teacher":
        scope_group_ids = get_group_ids_in_head_teacher_scope(db, current_user.id)
        if not scope_group_ids:
            return []
        query = query.filter(LessonRequest.group_id.in_(scope_group_ids))

    if status_filter:
        statuses = [s.strip() for s in status_filter.split(",") if s.strip()]
        if statuses:
            query = query.filter(LessonRequest.status.in_(statuses))

    requests = query.order_by(LessonRequest.created_at.desc()).all()
    return enrich_requests(requests, db)


@router.get("/pending-approval", response_model=List[LessonRequestSchema])
async def list_pending_approval(
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(_require_admin_or_head_teacher),
):
    """Pending requests awaiting head teacher or admin approval."""
    query = db.query(LessonRequest).filter(
        LessonRequest.status.in_(["pending", "pending_teacher"])
    )

    if current_user.role == "head_teacher":
        scope_group_ids = get_group_ids_in_head_teacher_scope(db, current_user.id)
        if not scope_group_ids:
            return []
        query = query.filter(LessonRequest.group_id.in_(scope_group_ids))

    requests = query.order_by(LessonRequest.created_at.desc()).all()
    return enrich_requests(requests, db)


@router.post("/{request_id}/approve", response_model=LessonRequestSchema)
async def approve_lesson_request(
    request_id: int,
    data: ResolveLessonRequestSchema,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Approve a lesson request – applies the substitution, reschedule, or cancel."""
    lr = db.query(LessonRequest).filter(LessonRequest.id == request_id).first()
    if not lr:
        raise HTTPException(status_code=404, detail="Request not found")
    if lr.status != "pending":
        raise HTTPException(status_code=400, detail="Request already resolved")
    if not user_can_resolve_request(db, current_user, lr.group_id):
        raise HTTPException(status_code=403, detail="Not authorized to approve this request")

    apply_approved_request(db, lr, current_user.id)

    lr.status = "approved"
    lr.admin_comment = data.admin_comment
    lr.resolved_at = datetime.now(timezone.utc)
    lr.resolved_by = current_user.id
    db.commit()
    db.refresh(lr)

    notify_resolution(db, lr, approved=True)
    return enrich_request(lr, db)


@router.post("/{request_id}/reject", response_model=LessonRequestSchema)
async def reject_lesson_request(
    request_id: int,
    data: ResolveLessonRequestSchema,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Reject a lesson request."""
    lr = db.query(LessonRequest).filter(LessonRequest.id == request_id).first()
    if not lr:
        raise HTTPException(status_code=404, detail="Request not found")
    if lr.status != "pending":
        raise HTTPException(status_code=400, detail="Request already resolved")
    if not user_can_resolve_request(db, current_user, lr.group_id):
        raise HTTPException(status_code=403, detail="Not authorized to reject this request")

    lr.status = "rejected"
    lr.admin_comment = data.admin_comment
    lr.resolved_at = datetime.now(timezone.utc)
    lr.resolved_by = current_user.id
    db.commit()
    db.refresh(lr)

    notify_resolution(db, lr, approved=False)
    return enrich_request(lr, db)


# =============================================================================
# TEACHER AVAILABILITY SEARCH
# =============================================================================

@router.get("/teachers/available")
async def get_available_teachers(
    datetime_str: str = Query(..., description="ISO datetime for the lesson slot"),
    group_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Find teachers available at a given time who have NOT opted out of substitutions."""
    if current_user.role not in ("teacher", "admin"):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        target_dt = datetime.fromisoformat(datetime_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid datetime format, use ISO format")

    window_start = target_dt - timedelta(minutes=30)
    window_end = target_dt + timedelta(minutes=90)

    query = db.query(UserInDB).filter(
        UserInDB.role == "teacher",
        UserInDB.is_active == True,
        UserInDB.no_substitutions == False,
        UserInDB.id != current_user.id,
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
                group_teachers = db.query(Group.teacher_id).filter(
                    Group.id.in_(relevant_group_ids)
                ).all()
                for t in group_teachers:
                    allowed_teacher_ids.add(t[0])

            course_teachers = db.query(Course.teacher_id).filter(
                Course.id.in_(course_ids),
                Course.teacher_id.isnot(None),
            ).all()
            for ct in course_teachers:
                allowed_teacher_ids.add(ct[0])

            if allowed_teacher_ids:
                query = query.filter(UserInDB.id.in_(allowed_teacher_ids))
            else:
                query = query.filter(UserInDB.id == -1)

    teachers = query.all()
    busy_teacher_ids = set()

    busy_events = db.query(Event).filter(
        Event.is_active == True,
        Event.teacher_id.isnot(None),
        Event.start_datetime < window_end,
        Event.end_datetime > window_start,
    ).all()
    for ev in busy_events:
        busy_teacher_ids.add(ev.teacher_id)

    busy_schedules = db.query(LessonSchedule).filter(
        LessonSchedule.is_active == True,
        LessonSchedule.scheduled_at >= window_start,
        LessonSchedule.scheduled_at < window_end,
    ).all()
    for sched in busy_schedules:
        if sched.group and sched.group.teacher_id:
            busy_teacher_ids.add(sched.group.teacher_id)

    available = []
    for t in teachers:
        if t.id not in busy_teacher_ids:
            available.append({"id": t.id, "name": t.name, "email": t.email})

    return {"available_teachers": available}


# =============================================================================
# TEACHER PREFERENCE
# =============================================================================

@router.put("/preferences/no-substitutions")
async def update_substitution_preference(
    enabled: bool = Query(..., description="True to opt-out from substitutions"),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(get_current_user_dependency),
):
    """Toggle the no_substitutions preference for the current teacher."""
    if current_user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can change this preference")

    current_user.no_substitutions = enabled
    db.commit()
    return {"detail": "Preference updated", "no_substitutions": enabled}
