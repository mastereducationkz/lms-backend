"""Student testimonials: photo + quote for the sales team, with a consent record.

Consent is a first-class part of the model, not a checkbox bolted on. The subjects are
frequently minors and the material is used in advertising, so every approved item must
be able to answer, months later: who agreed, to which channels, when, and which staff
member recorded it. Approval is refused unless that record exists.

Two rules the API enforces and the UI cannot bypass:

* ``approve`` requires ``consent_given`` and at least one permitted channel.
* ``revoke`` is always available and immediately removes the item from the marketing
  view, because consent that cannot be withdrawn is not consent.
"""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from src.auth.models import UserInDB
from src.config import get_db
from src.exams.models import TESTIMONIAL_CHANNELS, StudentTestimonial
from src.routes.auth import get_current_user_dependency
from src.services import storage_service

router = APIRouter()

# Who may collect and edit testimonials. Same set that already records exam results.
_EDIT_ROLES = {"curator", "head_curator", "admin"}
# Who may approve, revoke, and see the marketing view. Deliberately narrower than
# editing: the person who collected the material should not be the only check on it.
_APPROVE_ROLES = {"head_curator", "admin"}

_ALLOWED_PHOTO_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})
_PHOTO_MAX_BYTES = 8 * 1024 * 1024


def _require(user: UserInDB, allowed: set) -> None:
    if (user.role or "").strip().lower() not in allowed:
        raise HTTPException(status_code=403, detail="Access denied")


def _sniff_image(data: bytes) -> Optional[str]:
    """Type from magic bytes. A student photo is PII; do not trust the browser."""
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# --------------------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------------------

class TestimonialUpsert(BaseModel):
    student_id: int
    quote: Optional[str] = Field(None, max_length=4000)
    exam_result_id: Optional[int] = None
    consent_given: bool = False
    consent_channels: List[str] = []
    guardian_consent: bool = False
    consent_note: Optional[str] = None


class TestimonialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: int
    student_name: Optional[str] = None
    quote: Optional[str] = None
    has_photo: bool = False
    status: str
    consent_given: bool
    consent_channels: Optional[List[str]] = None
    guardian_consent: bool
    consent_note: Optional[str] = None
    consent_recorded_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    rejected_reason: Optional[str] = None
    is_marketing_ready: bool = False


def _to_out(row: StudentTestimonial, student_name: Optional[str] = None) -> TestimonialOut:
    return TestimonialOut(
        id=row.id,
        student_id=row.student_id,
        student_name=student_name,
        quote=row.quote,
        # The storage key is never returned; the photo is fetched through the
        # scope-checked endpoint below.
        has_photo=bool(row.photo_url),
        status=row.status,
        consent_given=bool(row.consent_given),
        consent_channels=row.consent_channels or [],
        guardian_consent=bool(row.guardian_consent),
        consent_note=row.consent_note,
        consent_recorded_at=row.consent_recorded_at,
        approved_at=row.approved_at,
        revoked_at=row.revoked_at,
        rejected_reason=row.rejected_reason,
        is_marketing_ready=row.is_marketing_ready,
    )


def _scoped_or_403(db: Session, user: UserInDB, student_id: int) -> None:
    """Testimonials follow the same row scope as everything else in this domain."""
    from src.exams.routes import _scoped_student_ids

    scoped = _scoped_student_ids(db, user, group_id=None)
    if scoped is not None and student_id not in set(scoped):
        raise HTTPException(status_code=403, detail="Access denied for this student")


# --------------------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------------------

@router.put("/testimonials", response_model=TestimonialOut)
def upsert_testimonial(
    payload: TestimonialUpsert,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Create or update a student's testimonial and its consent record.

    Editing anything about the consent resets an approved item back to pending: an
    approval attests to the material as it stood when it was reviewed, so changing the
    quote or the permitted channels afterwards has to be re-checked.
    """
    _require(current_user, _EDIT_ROLES)
    _scoped_or_403(db, current_user, payload.student_id)

    unknown = [c for c in payload.consent_channels if c not in TESTIMONIAL_CHANNELS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown consent channel(s): {', '.join(unknown)}. "
                   f"Allowed: {', '.join(TESTIMONIAL_CHANNELS)}.",
        )

    row = (
        db.query(StudentTestimonial)
        .filter(StudentTestimonial.student_id == payload.student_id)
        .first()
    )
    created = row is None
    if created:
        row = StudentTestimonial(student_id=payload.student_id, created_by=current_user.id)
        db.add(row)

    if row.revoked_at is not None:
        raise HTTPException(
            status_code=409,
            detail="This testimonial was revoked. Consent must be obtained again "
                   "before it can be edited.",
        )

    consent_changed = (
        bool(row.consent_given) != payload.consent_given
        or (row.consent_channels or []) != payload.consent_channels
        or bool(row.guardian_consent) != payload.guardian_consent
    )
    content_changed = (row.quote or "") != (payload.quote or "")

    row.quote = payload.quote
    row.exam_result_id = payload.exam_result_id
    row.consent_given = payload.consent_given
    row.consent_channels = payload.consent_channels
    row.guardian_consent = payload.guardian_consent
    row.consent_note = payload.consent_note

    if payload.consent_given and (created or consent_changed):
        row.consent_recorded_by = current_user.id
        row.consent_recorded_at = datetime.now(timezone.utc)
    if not payload.consent_given:
        row.consent_recorded_by = None
        row.consent_recorded_at = None

    if row.status == "approved" and (consent_changed or content_changed):
        row.status = "pending"
        row.approved_by = None
        row.approved_at = None
    elif row.status in (None, "draft"):
        row.status = "pending" if payload.consent_given else "draft"

    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/testimonials/{testimonial_id}/photo", response_model=TestimonialOut)
async def upload_testimonial_photo(
    testimonial_id: int,
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Attach the student's photo. Stored privately, never on a public prefix."""
    _require(current_user, _EDIT_ROLES)
    row = db.query(StudentTestimonial).filter(StudentTestimonial.id == testimonial_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Testimonial not found")
    _scoped_or_403(db, current_user, row.student_id)

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > _PHOTO_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Photo too large (max 8 MB)")
    sniffed = _sniff_image(data)
    if sniffed not in _ALLOWED_PHOTO_MIMES:
        raise HTTPException(status_code=400, detail="Upload a JPEG, PNG or WEBP image.")

    import uuid
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[sniffed]
    key = f"testimonial_media/{row.student_id}_{uuid.uuid4().hex}{ext}"
    storage_service.save(key, data, content_type=sniffed)

    row.photo_url = storage_service.stored_path(key)
    row.photo_uploaded_at = datetime.now(timezone.utc)
    # A new photo is new material, so a previous approval no longer covers it.
    if row.status == "approved":
        row.status = "pending"
        row.approved_by = None
        row.approved_at = None
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.get("/testimonials/{testimonial_id}/photo")
def get_testimonial_photo(
    testimonial_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Short-lived link to the photo, re-authorized per request."""
    _require(current_user, _EDIT_ROLES | _APPROVE_ROLES)
    row = db.query(StudentTestimonial).filter(StudentTestimonial.id == testimonial_id).first()
    if row is None or not row.photo_url:
        raise HTTPException(status_code=404, detail="No photo for this testimonial")
    _scoped_or_403(db, current_user, row.student_id)
    return RedirectResponse(url=storage_service.url_for(row.photo_url), status_code=307)


class ModerationAction(BaseModel):
    reason: Optional[str] = None


@router.post("/testimonials/{testimonial_id}/approve", response_model=TestimonialOut)
def approve_testimonial(
    testimonial_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Release a testimonial to the sales team.

    Refused without a consent record. This is the gate the whole model exists for: an
    approval without recorded permission is exactly the situation that cannot be
    defended later.
    """
    _require(current_user, _APPROVE_ROLES)
    row = db.query(StudentTestimonial).filter(StudentTestimonial.id == testimonial_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Testimonial not found")
    if row.revoked_at is not None:
        raise HTTPException(status_code=409, detail="This testimonial was revoked.")
    if not row.consent_given:
        raise HTTPException(
            status_code=400,
            detail="Consent has not been recorded, so this cannot be approved.",
        )
    if not (row.consent_channels or []):
        raise HTTPException(
            status_code=400,
            detail="Record at least one channel the student agreed to before approving.",
        )

    row.status = "approved"
    row.approved_by = current_user.id
    row.approved_at = datetime.now(timezone.utc)
    row.rejected_reason = None
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/testimonials/{testimonial_id}/reject", response_model=TestimonialOut)
def reject_testimonial(
    testimonial_id: int,
    payload: ModerationAction,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    _require(current_user, _APPROVE_ROLES)
    row = db.query(StudentTestimonial).filter(StudentTestimonial.id == testimonial_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Testimonial not found")
    row.status = "rejected"
    row.rejected_reason = payload.reason
    row.approved_by = None
    row.approved_at = None
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/testimonials/{testimonial_id}/revoke", response_model=TestimonialOut)
def revoke_testimonial(
    testimonial_id: int,
    payload: ModerationAction,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Withdraw consent. Terminal, and immediately removes the item from marketing.

    Available to editors as well as approvers: whoever hears "please take my photo
    down" must be able to act on it without waiting for someone else.
    """
    _require(current_user, _EDIT_ROLES | _APPROVE_ROLES)
    row = db.query(StudentTestimonial).filter(StudentTestimonial.id == testimonial_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Testimonial not found")
    _scoped_or_403(db, current_user, row.student_id)

    row.status = "revoked"
    row.revoked_by = current_user.id
    row.revoked_at = datetime.now(timezone.utc)
    row.revoked_reason = payload.reason
    row.consent_given = False
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.get("/testimonials", response_model=List[TestimonialOut])
def list_testimonials(
    marketing_ready: bool = Query(
        False,
        description="Only approved, consented, non-revoked items - what sales may use.",
    ),
    status: Optional[str] = Query(None),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Testimonials for students in the caller's scope."""
    _require(current_user, _EDIT_ROLES | _APPROVE_ROLES)
    from src.exams.routes import _scoped_student_ids

    scoped = _scoped_student_ids(db, current_user, group_id=None)
    query = db.query(StudentTestimonial, UserInDB.name).join(
        UserInDB, UserInDB.id == StudentTestimonial.student_id
    )
    if scoped is not None:
        if not scoped:
            return []
        query = query.filter(StudentTestimonial.student_id.in_(scoped))

    if marketing_ready:
        query = query.filter(
            StudentTestimonial.status == "approved",
            StudentTestimonial.consent_given == True,  # noqa: E712
            StudentTestimonial.revoked_at.is_(None),
        )
    elif status:
        query = query.filter(StudentTestimonial.status == status)

    return [_to_out(row, name) for row, name in query.order_by(UserInDB.name).all()]
