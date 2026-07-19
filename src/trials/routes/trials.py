from typing import Optional, List, Set

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config import get_db
from src.schemas.models import UserInDB, Course, Lesson, Module
from src.utils.permissions import require_admin_or_head_curator
from src.utils.auth_utils import hash_password  # match the import used in src/admin/routes/admin.py
from src.admin.routes.admin import generate_password, generate_student_id
from src.services.email_service import send_invite_email
from src.trials.models import TrialAccess, TRIAL_ACTIVE, TRIAL_REVOKED, TRIAL_CONVERTED
from src.trials.schemas import (
    TrialCreateRequest, TrialUpdateRequest, TrialSchema, TrialCreateResponse, effective_status,
)
from src.trials import services as trial_services

router = APIRouter()

_DUPLICATE_ACTIVE_DETAIL = "This prospect already has an active trial for this course — edit it instead"
_REACTIVATE_CONFLICT_DETAIL = "Another active trial exists for this course — revoke it first"


def _validate_lesson_ids(lesson_ids: List[int], course_lesson_ids: Set[int]) -> List[int]:
    ids = [int(x) for x in (lesson_ids or [])]
    if not ids:
        raise HTTPException(status_code=422, detail="Select at least one lesson")
    foreign = [x for x in ids if x not in course_lesson_ids]
    if foreign:
        raise HTTPException(status_code=422, detail=f"Lessons not in this course: {foreign}")
    return sorted(set(ids))


def _course_lesson_ids(db: Session, course_id: int) -> Set[int]:
    rows = (
        db.query(Lesson.id)
        .join(Module, Lesson.module_id == Module.id)
        .filter(Module.course_id == course_id)
        .all()
    )
    return {r[0] for r in rows}


def _to_schema(db: Session, grant: TrialAccess) -> TrialSchema:
    user = db.query(UserInDB).filter(UserInDB.id == grant.user_id).first()
    course = db.query(Course).filter(Course.id == grant.course_id).first()
    granted_by_name = None
    if grant.granted_by:
        gb = db.query(UserInDB).filter(UserInDB.id == grant.granted_by).first()
        granted_by_name = gb.name if gb else None
    return TrialSchema(
        id=grant.id,
        user_id=grant.user_id,
        user_email=user.email if user else "",
        user_name=user.name if user else "",
        course_id=grant.course_id,
        course_title=course.title if course else "",
        lesson_ids=[int(x) for x in (grant.lesson_ids or [])],
        expires_at=grant.expires_at,
        status=effective_status(grant),
        granted_by=grant.granted_by,
        granted_by_name=granted_by_name,
        prospect_note=grant.prospect_note,
        created_at=grant.created_at,
        revoked_at=grant.revoked_at,
    )


@router.post("/", response_model=TrialCreateResponse)
def create_trial(
    body: TrialCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    email = body.email.lower().strip()
    course = db.query(Course).filter(Course.id == body.course_id).first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found")
    lesson_ids = _validate_lesson_ids(body.lesson_ids, _course_lesson_ids(db, body.course_id))
    expires_at = trial_services._as_utc_naive(body.expires_at)

    user = db.query(UserInDB).filter(UserInDB.email == email).first()
    generated_password: Optional[str] = None
    if user and not user.is_trial:
        raise HTTPException(status_code=409, detail="Email belongs to an existing non-trial account")
    if user:
        # Flip any stale 'active'-status-past-deadline row first so the duplicate
        # check below and the status-only partial unique index agree exactly.
        trial_services.expire_stale_trials_for(db, user.id, body.course_id)
        existing = db.query(TrialAccess).filter(
            TrialAccess.user_id == user.id,
            TrialAccess.course_id == body.course_id,
            TrialAccess.status == TRIAL_ACTIVE,
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail=_DUPLICATE_ACTIVE_DETAIL)
        if trial_services.should_rotate_password(trial_services.get_active_trials(db, user.id)):
            password = generate_password()
            generated_password = password
            user.hashed_password = hash_password(password)  # fresh credentials for the new grant
        else:
            # Another live grant means their current credentials are in use —
            # keep them; admin can rotate explicitly via resend-invite.
            password = None
    else:
        password = generate_password()
        generated_password = password
        student_id = generate_student_id()
        while db.query(UserInDB).filter(UserInDB.student_id == student_id).first():
            student_id = generate_student_id()
        user = UserInDB(
            email=email,
            name=body.name,
            hashed_password=hash_password(password),
            role="student",
            student_id=student_id,
            is_active=True,
            is_trial=True,
            assignment_zero_completed=True,  # spec: trial users never enter the Assignment-Zero funnel
        )
        db.add(user)
        db.flush()

    grant = TrialAccess(
        user_id=user.id,
        course_id=body.course_id,
        lesson_ids=lesson_ids,
        expires_at=expires_at,
        status=TRIAL_ACTIVE,
        granted_by=current_user.id,
        prospect_note=body.prospect_note,
    )
    db.add(grant)
    try:
        db.commit()
    except IntegrityError:
        # Multi-worker race on the status-only partial unique index
        db.rollback()
        raise HTTPException(status_code=409, detail=_DUPLICATE_ACTIVE_DETAIL)
    db.refresh(grant)

    if body.send_invite and password is not None:
        background_tasks.add_task(send_invite_email, user.email, user.name or "", user.email, password)

    return TrialCreateResponse(trial=_to_schema(db, grant), generated_password=generated_password)


@router.get("/")
def list_trials(
    status: Optional[str] = Query(None),
    course_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    q = db.query(TrialAccess).order_by(TrialAccess.created_at.desc())
    if course_id:
        q = q.filter(TrialAccess.course_id == course_id)
    grants = q.all()
    out = [_to_schema(db, g) for g in grants]
    if status:
        out = [t for t in out if t.status == status]
    if search:
        s = search.lower()
        out = [t for t in out if s in t.user_email.lower() or s in t.user_name.lower()]
    return {"trials": out}


def _get_grant_or_404(db: Session, trial_id: int) -> TrialAccess:
    grant = db.query(TrialAccess).filter(TrialAccess.id == trial_id).first()
    if not grant:
        raise HTTPException(status_code=404, detail="Trial not found")
    return grant


@router.patch("/{trial_id}", response_model=TrialSchema)
def update_trial(
    trial_id: int,
    body: TrialUpdateRequest,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    grant = _get_grant_or_404(db, trial_id)
    if body.expires_at is not None:
        grant.expires_at = trial_services._as_utc_naive(body.expires_at)
        if grant.status in ("expired",) and trial_services.grant_is_active(grant):
            # Extending a lapsed trial re-activates it — but only one row per
            # (user, course) may hold the 'active' status (partial unique index).
            trial_services.expire_stale_trials_for(db, grant.user_id, grant.course_id)
            other_active = db.query(TrialAccess).filter(
                TrialAccess.user_id == grant.user_id,
                TrialAccess.course_id == grant.course_id,
                TrialAccess.status == TRIAL_ACTIVE,
                TrialAccess.id != grant.id,
            ).first()
            if other_active:
                raise HTTPException(status_code=409, detail=_REACTIVATE_CONFLICT_DETAIL)
            grant.status = TRIAL_ACTIVE
    if body.lesson_ids is not None:
        grant.lesson_ids = _validate_lesson_ids(body.lesson_ids, _course_lesson_ids(db, grant.course_id))
    if body.prospect_note is not None:
        grant.prospect_note = body.prospect_note
    try:
        db.commit()
    except IntegrityError:
        # Multi-worker race on the status-only partial unique index
        db.rollback()
        raise HTTPException(status_code=409, detail=_REACTIVATE_CONFLICT_DETAIL)
    db.refresh(grant)
    return _to_schema(db, grant)


@router.post("/{trial_id}/revoke", response_model=TrialSchema)
def revoke_trial(
    trial_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    grant = _get_grant_or_404(db, trial_id)
    if grant.status == TRIAL_CONVERTED:
        raise HTTPException(status_code=409, detail="Trial has already been converted — it can no longer be revoked")
    grant.status = TRIAL_REVOKED
    grant.revoked_at = trial_services.utcnow()
    db.commit()
    db.refresh(grant)
    return _to_schema(db, grant)


@router.post("/{trial_id}/resend-invite")
def resend_trial_invite(
    trial_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    grant = _get_grant_or_404(db, trial_id)
    user = db.query(UserInDB).filter(UserInDB.id == grant.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Trial user not found")
    password = generate_password()
    user.hashed_password = hash_password(password)
    db.commit()
    background_tasks.add_task(send_invite_email, user.email, user.name or "", user.email, password)
    return {"sent": True}


@router.post("/{trial_id}/convert", response_model=TrialSchema)
def convert_trial(
    trial_id: int,
    db: Session = Depends(get_db),
    current_user: UserInDB = Depends(require_admin_or_head_curator()),
):
    grant = _get_grant_or_404(db, trial_id)
    if grant.status in (TRIAL_REVOKED, TRIAL_CONVERTED):
        raise HTTPException(status_code=409, detail=f"Cannot convert a {grant.status} trial")
    grant.status = TRIAL_CONVERTED
    user = db.query(UserInDB).filter(UserInDB.id == grant.user_id).first()
    if user:
        user.is_trial = False  # now a real student; admin enrolls via the normal group flow
    db.commit()
    db.refresh(grant)
    return _to_schema(db, grant)
