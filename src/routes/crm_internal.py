"""Internal CRM ↔ LMS routes (service-key auth)."""
import logging
import os
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
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
from src.services.email_service import send_invite_email
from src.services.sso_broker import SsoBrokerError, mint_handoff, sso_broker_enabled
from src.utils.auth_utils import verify_lms_stored_password

logger = logging.getLogger(__name__)

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


class SendInviteBody(BaseModel):
    lms_user_id: int
    password: str


def _deliver_invite(user, password: str) -> dict:
    """Send a student their LMS credentials. Pure decision + send; safe to unit-test.

    Skips non-students and synthetic phone-only logins (…@import.local), which never
    reach a real inbox. Returns {sent, reason}."""
    if getattr(user, "role", None) != "student":
        return {"sent": False, "reason": "not_student"}
    email = (getattr(user, "email", "") or "").strip().lower()
    if not email or "@" not in email or email.endswith("@import.local"):
        return {"sent": False, "reason": "no_email"}
    send_invite_email(email, user.name or "", email, password)
    return {"sent": True, "reason": None}


# --- SSO Phase 0: teacher-login handoff (replaces CRM reading LMS password hashes) ---
#
# Today CRM authenticates LMS teachers by reading users.hashed_password directly.
# Under Phase 0, CRM instead posts the credential here; LMS (the owner) verifies it
# against its own users table and asks the broker to mint a short-lived handoff that
# CRM redeems. This removes CRM's cross-DB read of LMS password hashes. The whole
# path is gated by SSO_BROKER_ENABLED so it is fully dual-run with the legacy flow.


class IssueTeacherHandoffBody(BaseModel):
    email: str
    password: str


class IssueTeacherHandoffResponse(BaseModel):
    handoff: str
    expires_at: int


def _sso_teacher_roles() -> tuple[str, ...]:
    """Roles eligible for the teacher-login handoff (matches CRM's LMS_CRM_TEACHER_ROLES)."""
    raw = os.getenv("SSO_TEACHER_ROLES", "teacher,head_teacher")
    return tuple(r.strip() for r in raw.split(",") if r.strip())


def _teacher_handoff_authorized(user, password: str, roles: tuple[str, ...]) -> bool:
    """Pure decision: may this user receive a teacher handoff for the given password?

    Kept side-effect-free (no DB / no network) so it is unit-testable. Requires an
    active user whose role is eligible and whose stored password verifies.
    """
    if user is None or not getattr(user, "is_active", False):
        return False
    if getattr(user, "role", None) not in roles:
        return False
    algorithm = os.getenv("SSO_TEACHER_PASSWORD_ALGORITHM", "auto")
    return verify_lms_stored_password(password, getattr(user, "hashed_password", None), algorithm=algorithm)


def _teacher_handoff_mint_kwargs(user) -> dict:
    """Pure: the broker mint arguments for an authenticated teacher."""
    return {
        "sub": f"lms:{user.id}",
        "aud": "crm",
        "scope": ["crm:teacher"],
        "email": (getattr(user, "email", "") or None),
        "name": (getattr(user, "name", "") or ""),
    }


@router.post(
    "/issue-teacher-handoff",
    response_model=IssueTeacherHandoffResponse,
    dependencies=[Depends(_require_crm_internal_key)],
)
async def crm_issue_teacher_handoff(
    body: IssueTeacherHandoffBody,
    db: Session = Depends(get_db),
) -> IssueTeacherHandoffResponse:
    """Verify a teacher's LMS credential and return a broker-signed handoff for CRM."""
    if not sso_broker_enabled():
        raise HTTPException(status_code=503, detail="SSO broker handoff is disabled")

    email = (body.email or "").strip().lower()
    roles = _sso_teacher_roles()
    user = (
        db.query(UserInDB)
        .filter(func.lower(UserInDB.email) == email, UserInDB.role.in_(roles))
        .first()
    )
    if not _teacher_handoff_authorized(user, body.password, roles):
        # Uniform error: never distinguish "no such teacher" from "bad password".
        raise HTTPException(status_code=401, detail="Invalid credentials")

    try:
        result = mint_handoff(**_teacher_handoff_mint_kwargs(user))
    except SsoBrokerError as exc:
        logger.warning("teacher-handoff mint failed for %s: %s", email, exc)
        raise HTTPException(status_code=502, detail="SSO broker unavailable") from exc

    return IssueTeacherHandoffResponse(handoff=result["handoff"], expires_at=result["expires_at"])


@router.post(
    "/students/send-invite",
    dependencies=[Depends(_require_crm_internal_key)],
)
async def crm_send_student_invite(
    body: SendInviteBody,
    db: Session = Depends(get_db),
) -> dict:
    """Email a student their LMS login + password (invite). Called by CRM after it
    provisions a new LMS account for a converted lead."""
    user = db.query(UserInDB).filter(UserInDB.id == body.lms_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="LMS user not found")
    return _deliver_invite(user, body.password)


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
