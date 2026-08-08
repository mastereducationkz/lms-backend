"""Exam-results and Bluebook API.

Authorization notes
-------------------
Every read here is row-scoped through :mod:`src.utils.scope`, so list, detail, grid and
export all share one definition of "which rows may this user see". Export in particular
re-derives scope from the authenticated user and cannot be widened by changing a query
parameter.

Response models are purpose-built (:mod:`src.exams.schemas`). None of them inherit from
``AssignmentZeroSubmissionSchema``, which declares ``college_board_email`` /
``college_board_password``; nothing in this module may return those fields.
"""
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import os
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from src.assignments.exam_dates import (
    SAT_DATES_SOURCE_URL,
    SAT_DATES_VERIFIED_AT,
    SAT_TEST_DATES,
    format_sat_label,
    get_nearest_sat_date,
)
from src.assignments.models import AssignmentZeroSubmission
from src.auth.models import UserInDB
from src.config import get_db
from src.courses.models import Group, GroupStudent
from src.exams.bluebook_pdf import BluebookReportError, names_are_similar, parse_report_pdf
from src.exams.models import BluebookResult, ExamResult
from src.exams.schemas import (
    ExamResultCreate,
    ExamResultOut,
    ExamResultRow,
    ExamResultUpdate,
    PlannedDateUpdate,
)
from src.exams import services as exam_services
from src.exams.tracks import resolve_student_tracks
from src.exams.exports import build_bluebook_grid_workbook, build_exam_results_workbook
from src.routes.auth import get_current_user_dependency
from src.services import storage_service
from src.utils.scope import UNRESTRICTED, can_view_group, visible_group_ids

router = APIRouter()

# Testimonials live in their own module but mount under the same /exams prefix, so the
# whole exam-results workflow - results, evidence and marketing material - is one API.
from src.exams.testimonials import router as _testimonials_router  # noqa: E402
router.include_router(_testimonials_router)

# Who may READ exam data, subject to row scope.
_READ_ROLES = {"teacher", "curator", "head_teacher", "head_curator", "admin"}
# Who may CREATE or CORRECT an official result. Teachers are read-only here:
# an official exam score is a record of fact, owned by the curator team.
_WRITE_ROLES = {"curator", "head_curator", "admin"}

# Exam boards release scores about two weeks after the sitting, so a result is chased
# from planned_date + 13d. Same offset the curator task scheduler applies, kept as a
# named constant here instead of the magic 13 that was duplicated in three places.
ASK_RESULT_AFTER_DAYS = 13


def _require(user: UserInDB, allowed: set) -> None:
    if (user.role or "").strip().lower() not in allowed:
        raise HTTPException(status_code=403, detail="Access denied")


# --------------------------------------------------------------------------------------
# Official SAT dates
# --------------------------------------------------------------------------------------

@router.get("/sat-dates")
def list_sat_official_dates(
    include_anticipated: bool = Query(
        False,
        description="Include College Board's provisional 'Anticipated' dates. "
                    "Off by default so pickers never present them as settled.",
    ),
    include_past: bool = Query(
        True,
        description="Include administrations that have already happened, needed for "
                    "recording historical results.",
    ),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """The canonical SAT calendar.

    This is the single source of truth the frontend must read instead of hard-coding
    dates. Two independent frontend copies previously projected the 2025-26
    day-of-month onto the current year, making every 2026-27 date wrong by 1-8 days.

    ``status`` distinguishes College Board's published schedule from the dates it lists
    under "Anticipated 2027-28 Test Dates". Anticipated dates are never returned unless
    explicitly requested, and are always labelled.
    """
    today = date.today()
    entries = []
    for entry in SAT_TEST_DATES:
        if entry.status == "anticipated" and not include_anticipated:
            continue
        if not include_past and entry.test_date < today:
            continue
        entries.append({
            "test_date": entry.test_date.isoformat(),
            "status": entry.status,
            "label": entry.test_date.strftime("%B %-d, %Y"),
            "month": entry.test_date.month,
            "year": entry.test_date.year,
            "registration_deadline": (
                entry.registration_deadline.isoformat() if entry.registration_deadline else None
            ),
            "change_deadline": (
                entry.change_deadline.isoformat() if entry.change_deadline else None
            ),
            "is_past": entry.test_date < today,
        })

    nearest = get_nearest_sat_date(today)
    return {
        "dates": entries,
        # None once the confirmed list runs out. Clients must render "no upcoming date"
        # rather than counting down to a date in the past.
        "next_confirmed_date": nearest.isoformat() if nearest else None,
        "source_url": SAT_DATES_SOURCE_URL,
        "verified_at": SAT_DATES_VERIFIED_AT.isoformat(),
    }


@router.get("/my-tracks")
def get_my_tracks(
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """The exam tracks the signed-in student is on.

    Single source of truth for track entitlement, shared with the dashboard countdown.
    The platform tiles previously derived this in the frontend from a different group
    query with different filters, so the same student could be a SAT student to the
    countdown and not to the tiles - visibly contradicting itself on one screen.
    """
    if (current_user.role or "").strip().lower() != "student":
        return {"tracks": []}
    return {"tracks": resolve_student_tracks(db, current_user)}


# --------------------------------------------------------------------------------------
# Exam results
# --------------------------------------------------------------------------------------

def _scoped_student_ids(
    db: Session,
    user: UserInDB,
    *,
    group_id: Optional[int],
) -> Optional[List[int]]:
    """Student ids this user may see, optionally narrowed to one group.

    Returns ``None`` for unrestricted users with no group filter. Raises 403 when a
    caller asks for a group outside their scope, rather than silently returning an
    empty list - a silent empty result hides an authorization error from the operator.
    """
    scope = visible_group_ids(user, db)

    if group_id is not None:
        if not can_view_group(user, db, group_id):
            raise HTTPException(status_code=403, detail="Access denied for this group")
        group_ids = [group_id]
    elif scope is UNRESTRICTED:
        return None
    else:
        group_ids = list(scope)

    if not group_ids:
        return []

    rows = (
        db.query(GroupStudent.student_id)
        .filter(GroupStudent.group_id.in_(group_ids))
        .distinct()
        .all()
    )
    return [r[0] for r in rows]


def _collect_result_rows(
    db: Session,
    user: UserInDB,
    *,
    exam_type: str,
    group_id: Optional[int],
    date_field: str,
    date_from: Optional[date],
    date_to: Optional[date],
    exact_date: Optional[date],
    status: Optional[str],
    search: Optional[str],
    limit: int,
    offset: int,
) -> List[ExamResultRow]:
    """Shared row builder for the grid and its export.

    The export calls this with the same arguments the screen used, so the two can never
    diverge on either filtering or authorization.
    """
    student_ids = _scoped_student_ids(db, user, group_id=group_id)
    if student_ids is not None and not student_ids:
        return []

    # --- resolve the set of students in scope -----------------------------------
    student_query = db.query(UserInDB).filter(
        UserInDB.role == "student",
        UserInDB.is_trial == False,  # noqa: E712
    )
    if student_ids is not None:
        student_query = student_query.filter(UserInDB.id.in_(student_ids))
    if search:
        like = f"%{search.strip()}%"
        student_query = student_query.filter(UserInDB.name.ilike(like))

    students = student_query.order_by(UserInDB.name).offset(offset).limit(limit).all()
    if not students:
        return []

    ids = [s.id for s in students]
    contacts = exam_services.resolve_student_contacts(db, ids)
    planned = exam_services.resolve_planned_dates(db, ids, exam_type)

    result_query = db.query(ExamResult).filter(
        ExamResult.student_id.in_(ids),
        ExamResult.exam_type == exam_type,
        ExamResult.is_superseded == False,  # noqa: E712
    )
    if status:
        result_query = result_query.filter(ExamResult.status == status)
    # "actual" filters the date the exam was sat; "planned" filters the intended
    # administration held on Assignment Zero. Both are explicit, never inferred.
    if date_field == "actual":
        if exact_date is not None:
            result_query = result_query.filter(ExamResult.test_date == exact_date)
        if date_from is not None:
            result_query = result_query.filter(ExamResult.test_date >= date_from)
        if date_to is not None:
            result_query = result_query.filter(ExamResult.test_date <= date_to)

    attempts_by_student: dict = {}
    for r in result_query.order_by(ExamResult.test_date.asc()).all():
        attempts_by_student.setdefault(r.student_id, []).append(r)
    # Ascending insert then reverse => newest first, and latest[...] is the newest.
    latest = {sid: rows_[-1] for sid, rows_ in attempts_by_student.items()}

    group_names = _group_names_for_students(db, ids, scope=visible_group_ids(user, db))
    today = date.today()

    rows: List[ExamResultRow] = []
    for s in students:
        planned_date = planned.get(s.id)

        if date_field == "planned":
            if exact_date is not None and planned_date != exact_date:
                continue
            if date_from is not None and (planned_date is None or planned_date < date_from):
                continue
            if date_to is not None and (planned_date is None or planned_date > date_to):
                continue

        result = latest.get(s.id)
        if date_field == "actual" and (date_from or date_to or exact_date) and result is None:
            # Filtering by when the exam was sat implies "has a result in that window".
            continue

        contact = contacts.get(s.id)
        if contact is None:
            continue

        group_id_value, group_name = group_names.get(s.id, (None, None))
        student_attempts = attempts_by_student.get(s.id, [])

        # Triage, so the daily "who do I chase" workflow lives on this screen rather
        # than a separate page. Results are collected ~13 days after the exam, which is
        # when scores are released; the same offset the curator task scheduler uses.
        ask_on = planned_date + timedelta(days=ASK_RESULT_AFTER_DAYS) if planned_date else None
        if result is not None:
            triage = "completed"
        elif planned_date is None:
            triage = "unscheduled"
        elif ask_on is not None and today > ask_on:
            triage = "overdue"
        elif ask_on is not None and today >= ask_on:
            triage = "due"
        else:
            triage = "pending"

        rows.append(ExamResultRow(
            student=contact,
            group_id=group_id_value,
            group_name=group_name,
            planned_test_date=planned_date,
            ask_result_on=ask_on,
            triage_status=triage,
            result=(
                ExamResultOut.model_validate(exam_services._with_proof_flag(result))
                if result else None
            ),
            attempts=[
                ExamResultOut.model_validate(exam_services._with_proof_flag(a))
                for a in reversed(student_attempts)
            ],
        ))
    return rows


def _group_names_for_students(
    db: Session,
    student_ids: List[int],
    scope: Optional[List[int]] = None,
) -> dict:
    """One live group per student, for display. Batched.

    Prefers a group inside the caller's own scope. Students commonly belong to more
    than one group, and picking an arbitrary one meant a teacher's correctly-scoped
    list displayed OTHER teachers' group names - which reads exactly like a row-scope
    leak even though the student set was right.
    """
    rows = (
        db.query(GroupStudent.student_id, Group.id, Group.name)
        .join(Group, Group.id == GroupStudent.group_id)
        .filter(
            GroupStudent.student_id.in_(student_ids),
            Group.is_active == True,  # noqa: E712
            Group.is_over == False,  # noqa: E712
        )
        .all()
    )
    in_scope = set(scope) if scope else None
    out: dict = {}
    for student_id, gid, gname in rows:
        current = out.get(student_id)
        if current is None:
            out[student_id] = (gid, gname)
        elif in_scope is not None and gid in in_scope and current[0] not in in_scope:
            # A group the caller owns beats one they merely happen to see.
            out[student_id] = (gid, gname)
    return out


@router.get("/results", response_model=List[ExamResultRow])
def list_exam_results(
    exam_type: str = Query("sat", pattern="^(sat|ielts|nuet)$"),
    group_id: Optional[int] = None,
    date_field: str = Query(
        "planned",
        pattern="^(planned|actual)$",
        description="Which date the date filters apply to: the planned/expected "
                    "administration, or the date the exam was actually sat.",
    ),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    exact_date: Optional[date] = Query(
        None, description="Exact official administration date, e.g. a cohort selector."
    ),
    status: Optional[str] = Query(None, pattern="^(reported|verified|rejected)$"),
    search: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Authorized, row-scoped exam-results grid with contact details."""
    _require(current_user, _READ_ROLES)
    return _collect_result_rows(
        db, current_user,
        exam_type=exam_type, group_id=group_id, date_field=date_field,
        date_from=date_from, date_to=date_to, exact_date=exact_date,
        status=status, search=search, limit=limit, offset=offset,
    )


@router.get("/results/export")
def export_exam_results(
    exam_type: str = Query("sat", pattern="^(sat|ielts|nuet)$"),
    group_id: Optional[int] = None,
    date_field: str = Query("planned", pattern="^(planned|actual)$"),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    exact_date: Optional[date] = None,
    status: Optional[str] = Query(None, pattern="^(reported|verified|rejected)$"),
    search: Optional[str] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """XLSX of exactly what the caller can see on screen.

    Same role gate, same row scope and same filters as ``GET /results``; the export
    cannot reach a row the grid would not show. Bounded at 5000 rows because the
    workbook is built in memory.
    """
    _require(current_user, _READ_ROLES)
    rows = _collect_result_rows(
        db, current_user,
        exam_type=exam_type, group_id=group_id, date_field=date_field,
        date_from=date_from, date_to=date_to, exact_date=exact_date,
        status=status, search=search, limit=5000, offset=0,
    )
    buffer = build_exam_results_workbook(rows, exam_type=exam_type)
    filename = f"exam-results_{exam_type}_{date.today().isoformat()}.xlsx"
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/results", response_model=ExamResultOut, status_code=201)
def create_exam_result(
    payload: ExamResultCreate,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Record an exam attempt. Multiple attempts per student are expected."""
    _require(current_user, _WRITE_ROLES)

    scoped = _scoped_student_ids(db, current_user, group_id=None)
    if scoped is not None and payload.student_id not in set(scoped):
        raise HTTPException(status_code=403, detail="Access denied for this student")

    existing = (
        db.query(ExamResult)
        .filter(
            ExamResult.student_id == payload.student_id,
            ExamResult.exam_type == payload.exam_type,
            ExamResult.test_date == payload.test_date,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="A result for this student, exam and date already exists. "
                   "Supersede it instead of creating a duplicate.",
        )

    result = ExamResult(
        student_id=payload.student_id,
        exam_type=payload.exam_type,
        test_date=payload.test_date,
        total_score=payload.total_score,
        verbal_score=payload.verbal_score,
        math_score=payload.math_score,
        listening_band=payload.listening_band,
        reading_band=payload.reading_band,
        writing_band=payload.writing_band,
        speaking_band=payload.speaking_band,
        proof_url=payload.proof_url,
        notes=payload.notes,
        status="reported",
        source="staff",
        recorded_by=current_user.id,
        recorded_at=datetime.now(timezone.utc),
    )
    db.add(result)
    db.flush()

    # Mirror the newest attempt into the Assignment Zero scalars.
    #
    # Those columns remain the "latest result" cache that older screens read, and the
    # curator task scheduler closes its "collect the result" task by checking
    # sat_result_score / ielts_result_score. Writing only to exam_results would leave
    # those tasks open forever and make the old screens look empty.
    _mirror_latest_to_assignment_zero(db, payload.student_id, payload.exam_type)

    db.commit()
    db.refresh(result)
    return ExamResultOut.model_validate(exam_services._with_proof_flag(result))


def _mirror_latest_to_assignment_zero(db: Session, student_id: int, exam_type: str) -> None:
    """Keep the Assignment Zero scalars in step with the newest live attempt."""
    if exam_type not in ("sat", "ielts"):
        return  # Assignment Zero has no NUET columns

    submission = (
        db.query(AssignmentZeroSubmission)
        .filter(AssignmentZeroSubmission.user_id == student_id)
        .first()
    )
    if submission is None:
        return

    newest = (
        db.query(ExamResult)
        .filter(
            ExamResult.student_id == student_id,
            ExamResult.exam_type == exam_type,
            ExamResult.is_superseded == False,  # noqa: E712
            ExamResult.status != "rejected",
        )
        .order_by(ExamResult.test_date.desc())
        .first()
    )
    if newest is None:
        return

    # Stored as a plain number so the legacy backfill regex and the old screens can
    # both read it back unambiguously.
    score_text = str(int(newest.total_score)) if newest.total_score == int(newest.total_score) \
        else str(newest.total_score)
    if exam_type == "sat":
        submission.sat_result_score = score_text
        submission.sat_result_test_date = newest.test_date
    else:
        submission.ielts_result_score = score_text
        submission.ielts_result_test_date = newest.test_date


@router.patch("/results/{result_id}", response_model=ExamResultOut)
def update_exam_result(
    result_id: int,
    payload: ExamResultUpdate,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Verify, reject, annotate or supersede an existing result.

    Score corrections deliberately are NOT possible here: a wrong score is superseded
    and re-entered so the original survives. There is no audit-log subsystem in this
    codebase, so overwriting would destroy the only record of what was reported.
    """
    _require(current_user, _WRITE_ROLES)

    result = db.query(ExamResult).filter(ExamResult.id == result_id).first()
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found")

    scoped = _scoped_student_ids(db, current_user, group_id=None)
    if scoped is not None and result.student_id not in set(scoped):
        raise HTTPException(status_code=403, detail="Access denied for this student")

    if payload.status is not None:
        result.status = payload.status
        if payload.status == "verified":
            result.verified_by = current_user.id
            result.verified_at = datetime.now(timezone.utc)
    if payload.notes is not None:
        result.notes = payload.notes
    if payload.proof_url is not None:
        result.proof_url = payload.proof_url
        result.proof_uploaded_at = datetime.now(timezone.utc)
    if payload.is_superseded is not None:
        result.is_superseded = payload.is_superseded

    db.commit()
    db.refresh(result)
    return ExamResultOut.model_validate(exam_services._with_proof_flag(result))


# --------------------------------------------------------------------------------------
# Proof of result (private evidence)
# --------------------------------------------------------------------------------------

# A score report is either a screenshot or a PDF export from the exam board.
_ALLOWED_PROOF_MIMES = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp", "application/pdf",
})
_PROOF_MAX_BYTES = 10 * 1024 * 1024


def _sniff_proof_mime(contents: bytes) -> Optional[str]:
    """Detect the real type from magic bytes.

    Content-Type is client-supplied and trivially spoofed, and extension checks are
    worse. This mirrors the Assignment Zero screenshot validator - the only upload in
    this codebase that actually sniffs - and adds PDF.
    """
    if len(contents) >= 3 and contents[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(contents) >= 8 and contents[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(contents) >= 6 and contents[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(contents) >= 12 and contents[:4] == b"RIFF" and contents[8:12] == b"WEBP":
        return "image/webp"
    if len(contents) >= 5 and contents[:5] == b"%PDF-":
        return "application/pdf"
    return None


def _load_result_in_scope(db: Session, user: UserInDB, result_id: int) -> ExamResult:
    """Fetch a result, 404 if missing and 403 if the caller may not see the student."""
    result = db.query(ExamResult).filter(ExamResult.id == result_id).first()
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found")
    scoped = _scoped_student_ids(db, user, group_id=None)
    if scoped is not None and result.student_id not in set(scoped):
        raise HTTPException(status_code=403, detail="Access denied for this student")
    return result


@router.post("/results/{result_id}/proof", response_model=ExamResultOut)
async def upload_result_proof(
    result_id: int,
    file: UploadFile = File(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Attach a score report to a result.

    Stored under the PRIVATE ``exam_proof/`` prefix with a uuid filename, so it is never
    served by the unauthenticated /uploads route the way assignment_zero screenshots are.
    Retrieval goes through GET .../proof, which re-checks row scope.
    """
    _require(current_user, _WRITE_ROLES)
    result = _load_result_in_scope(db, current_user, result_id)

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(contents) > _PROOF_MAX_BYTES:
        raise HTTPException(status_code=400, detail="File too large (max 10 MB)")

    sniffed = _sniff_proof_mime(contents)
    if sniffed not in _ALLOWED_PROOF_MIMES:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a JPEG, PNG, GIF, WEBP or PDF.",
        )

    ext = ".pdf" if sniffed == "application/pdf" else "." + sniffed.split("/")[1]
    if ext == ".jpeg":
        ext = ".jpg"
    # uuid, not the client filename: the client name is attacker-controlled and several
    # existing endpoints interpolate it straight into a storage key.
    key = f"exam_proof/{result.student_id}_{uuid.uuid4().hex}{ext}"
    storage_service.save(key, contents, content_type=sniffed)

    result.proof_url = storage_service.stored_path(key)
    result.proof_uploaded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(result)
    return ExamResultOut.model_validate(exam_services._with_proof_flag(result))


def _serve_private_file(key: str, label: str) -> Response:
    """Stream a private object through the API rather than redirecting to storage.

    A 307 to a presigned S3 URL looks tempting and works for a top-level navigation,
    but the client fetches these with XHR (it has to, to send the Authorization
    header), and the browser follows the redirect to s3.amazonaws.com, which sends no
    Access-Control-Allow-Origin. The request then dies with a CORS error even though
    the presigned URL itself is valid.

    Serving the bytes from this origin keeps the response inside the API's existing
    CORS allow-list. Same reason HLS video is streamed here instead of redirected.
    Both file types are size-capped on upload (10 MB proof, 8 MB photo), so reading
    them into memory is bounded.
    """
    data = storage_service.read(key)
    if data is None:
        raise HTTPException(status_code=404, detail=f"The {label} file could not be found in storage")

    media_type = storage_service.content_type_for(key)
    return Response(
        content=data,
        media_type=media_type,
        headers={
            # inline so a PDF or image opens in the tab instead of downloading.
            "Content-Disposition": f'inline; filename="{label}{os.path.splitext(key)[1]}"',
            # Private material: never let a shared cache hold on to it.
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/results/{result_id}/proof")
def get_result_proof(
    result_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Short-lived link to the score report, re-authorized on every request.

    Read roles may open proof for students in their own scope, so a teacher sees their
    own students' reports and nobody else's. The storage key itself is never returned in
    list payloads - only this endpoint hands one out, and only after a scope check.
    """
    _require(current_user, _READ_ROLES)
    result = _load_result_in_scope(db, current_user, result_id)
    if not result.proof_url:
        raise HTTPException(status_code=404, detail="No proof uploaded for this result")

    return _serve_private_file(result.proof_url, "proof")


# --------------------------------------------------------------------------------------
# Planned / expected exam date
# --------------------------------------------------------------------------------------

@router.patch("/planned-date", response_model=ExamResultRow)
def update_planned_date(
    payload: PlannedDateUpdate,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Reschedule a student's expected exam date.

    Writes to Assignment Zero, which owns planned dates - this domain never keeps a
    second calendar. Unlike the legacy Assignment Zero endpoint, rescheduling does NOT
    null an existing result: with attempt history a past result stays valid evidence of
    what happened, and destroying it to move a future date loses real data.
    """
    _require(current_user, _WRITE_ROLES)

    scoped = _scoped_student_ids(db, current_user, group_id=None)
    if scoped is not None and payload.student_id not in set(scoped):
        raise HTTPException(status_code=403, detail="Access denied for this student")

    student = db.query(UserInDB).filter(UserInDB.id == payload.student_id).first()
    if student is None:
        raise HTTPException(status_code=404, detail="Student not found")

    # Creates a stub row when the student has no Assignment Zero record, so staff are
    # never blocked from scheduling a date they already know - see the helper's docstring.
    from src.assignments.routes.assignment_zero import _get_or_create_planned_date_row

    submission = _get_or_create_planned_date_row(db, student)

    if payload.exam_type == "sat":
        submission.sat_planned_test_date = payload.planned_test_date
        # Keep the legacy human-readable string in step, since other screens read it.
        submission.sat_target_date = format_sat_label(payload.planned_test_date)
    elif payload.exam_type == "ielts":
        submission.ielts_planned_test_date = payload.planned_test_date
        submission.ielts_target_date = format_sat_label(payload.planned_test_date)
    else:
        raise HTTPException(
            status_code=400,
            detail="Planned dates are only tracked for SAT and IELTS.",
        )

    db.commit()

    rows = _collect_result_rows(
        db, current_user, exam_type=payload.exam_type, group_id=None,
        date_field="planned", date_from=None, date_to=None, exact_date=None,
        status=None, search=None, limit=1000, offset=0,
    )
    for row in rows:
        if row.student.student_id == payload.student_id:
            return row
    raise HTTPException(status_code=404, detail="Student not found after update")


# --------------------------------------------------------------------------------------
# Bluebook: official PDF report
# --------------------------------------------------------------------------------------

@router.post("/bluebook/parse-report")
async def parse_bluebook_report(
    file: UploadFile = File(...),
    expected_test_number: Optional[int] = Query(
        None, description="The test number the homework asked for, if known."
    ),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Validate and read a College Board Bluebook practice score report.

    Students submit the official PDF; the scores come from the parse, never from
    anything typed into the form. The PDF is stored under the private
    ``bluebook_report/`` prefix and its key returned, so the submission references the
    file rather than carrying scores the client could alter. The submit handler
    RE-PARSES that stored file, so even a tampered submission payload cannot change a
    recorded score.

    Parsing is pure regex over the PDF's text layer - deterministic, no AI. A report
    that cannot be read with certainty is refused rather than guessed at.
    """
    data = await file.read()
    try:
        report = parse_report_pdf(data)
    except BluebookReportError as exc:
        # The parser's messages are written for students; pass them through as-is.
        raise HTTPException(status_code=400, detail=str(exc))

    if expected_test_number is not None and report.test_number != expected_test_number:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This report is for SAT Practice {report.test_number}, but the homework "
                f"asks for Bluebook Test #{expected_test_number}. Upload the report for "
                f"the assigned test."
            ),
        )

    name_matches = names_are_similar(
        report.student_name, current_user.official_full_name or current_user.name
    )

    key = f"bluebook_report/{current_user.id}_{uuid.uuid4().hex}.pdf"
    storage_service.save(key, data, content_type="application/pdf")

    return {
        "report_key": storage_service.stored_path(key),
        "test_number": report.test_number,
        "verbal_score": report.verbal_score,
        "math_score": report.math_score,
        "total_score": report.total_score,
        "report_date": report.report_date.isoformat() if report.report_date else None,
        "student_name": report.student_name,
        # Surfaced so staff can spot a report submitted on someone else's behalf. It
        # never blocks: Latin/Cyrillic spelling differences are routine here.
        "name_matches": name_matches,
    }


@router.get("/bluebook/report")
def get_bluebook_report(
    assignment_id: int = Query(...),
    student_id: int = Query(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """The official College Board PDF a student submitted, for staff review.

    Scores are parsed from this file and the student cannot edit them, so whoever
    grades the work has to be able to see the source document - otherwise "the report
    says 720" is unverifiable. Streamed through the API, never redirected to storage.
    """
    _require(current_user, _READ_ROLES)

    row = (
        db.query(BluebookResult)
        .filter(
            BluebookResult.assignment_id == assignment_id,
            BluebookResult.student_id == student_id,
        )
        .first()
    )
    if row is None or not row.report_url:
        raise HTTPException(status_code=404, detail="No report was submitted for this task")
    if row.group_id is not None and not can_view_group(current_user, db, row.group_id):
        raise HTTPException(status_code=403, detail="Access denied for this group")

    return _serve_private_file(row.report_url, "bluebook-report")


@router.get("/bluebook/result")
def get_bluebook_result(
    assignment_id: int = Query(...),
    student_id: int = Query(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """The parsed Bluebook result for one submission, so the grader can see and
    correct what was read from the report."""
    _require(current_user, _READ_ROLES)

    row = (
        db.query(BluebookResult)
        .filter(
            BluebookResult.assignment_id == assignment_id,
            BluebookResult.student_id == student_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No Bluebook result for this submission")
    if row.group_id is not None and not can_view_group(current_user, db, row.group_id):
        raise HTTPException(status_code=403, detail="Access denied for this group")

    return {
        "id": row.id,
        "test_number": row.test_number,
        "verbal_score": row.verbal_score,
        "math_score": row.math_score,
        "total_score": row.total_score,
        "report_date": row.report_date.isoformat() if row.report_date else None,
        "report_student_name": row.report_student_name,
        "report_name_matches": row.report_name_matches,
        "has_report": bool(row.report_url),
        "overridden_at": row.overridden_at.isoformat() if row.overridden_at else None,
        "override_reason": row.override_reason,
    }


class BluebookOverride(BaseModel):
    """Staff correction of a parsed Bluebook score."""

    verbal_score: int
    math_score: int
    reason: str = Field(..., min_length=3, max_length=500)


@router.patch("/bluebook/results/{result_id}")
def override_bluebook_result(
    result_id: int,
    payload: BluebookOverride,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Correct a parsed Bluebook score.

    Students cannot edit a parsed score at all, so this is the escape hatch for a
    genuine parse failure. It records who changed it, when, and why - without that the
    override would be indistinguishable from the parsed value it replaced.
    """
    _require(current_user, {"teacher", "curator", "head_curator", "head_teacher", "admin"})

    row = db.query(BluebookResult).filter(BluebookResult.id == result_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Result not found")
    if row.group_id is not None and not can_view_group(current_user, db, row.group_id):
        raise HTTPException(status_code=403, detail="Access denied for this group")

    for label, value in (("verbal_score", payload.verbal_score), ("math_score", payload.math_score)):
        if not (200 <= value <= 800) or value % 10 != 0:
            raise HTTPException(
                status_code=400,
                detail=f"{label} must be between 200 and 800 in steps of 10.",
            )

    row.verbal_score = payload.verbal_score
    row.math_score = payload.math_score
    # Always derived, exactly as on the report.
    row.total_score = payload.verbal_score + payload.math_score
    row.overridden_by = current_user.id
    row.overridden_at = datetime.now(timezone.utc)
    row.override_reason = payload.reason
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "verbal_score": row.verbal_score,
        "math_score": row.math_score,
        "total_score": row.total_score,
        "overridden_at": row.overridden_at.isoformat() if row.overridden_at else None,
        "override_reason": row.override_reason,
    }


# --------------------------------------------------------------------------------------
# Bluebook grid
# --------------------------------------------------------------------------------------

def _scoped_group_options(
    db: Session,
    user: UserInDB,
    *,
    programs: Optional[List[str]],
    search: Optional[str],
) -> List[dict]:
    """Live groups in the caller's scope, optionally narrowed by program.

    Shared by the exam-results and Bluebook group pickers so both resolve scope the
    same way. Never reuse ``GET /users/groups/me`` for this: it implements only the
    student, teacher and curator branches and returns [] for admin, head_teacher and
    head_curator.

    The name fallback exists because the backfill migration never set ``nuet``, so
    legacy groups can still carry ``general_english``. The word boundary keeps a
    "Saturday" group from being read as SAT.
    """
    scope = visible_group_ids(user, db)
    teacher = aliased(UserInDB)
    query = (
        db.query(Group, teacher)
        .outerjoin(teacher, teacher.id == Group.teacher_id)
        .filter(
            Group.is_active == True,  # noqa: E712
            Group.is_over == False,  # noqa: E712
        )
    )

    if programs:
        clauses = []
        for program in programs:
            clauses.append(func.lower(Group.program_type) == program)
            clauses.append(Group.name.op("~*")(rf"\y{program}\y"))
        query = query.filter(or_(*clauses))

    if scope is not UNRESTRICTED:
        if not scope:
            return []
        query = query.filter(Group.id.in_(scope))

    if search and search.strip():
        like = f"%{search.strip()}%"
        query = query.filter(or_(
            Group.name.ilike(like),
            teacher.name.ilike(like),
            teacher.official_full_name.ilike(like),
        ))

    pairs = query.order_by(Group.name).all()
    return [
        {
            "id": g.id,
            "name": g.name,
            "program_type": g.program_type,
            "teacher_id": g.teacher_id,
            "teacher_name": (t.official_full_name or t.name) if t else None,
        }
        for g, t in pairs
    ]


@router.get("/groups")
def list_exam_groups(
    program: Optional[str] = Query(
        None,
        description="Restrict to one program (sat|ielts|nuet). Omit for all programs.",
    ),
    search: Optional[str] = Query(
        None, description="Free text matched against group name AND teacher name."
    ),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Groups the caller may filter exam results by, across all programs."""
    _require(current_user, _READ_ROLES)
    programs = [program.strip().lower()] if program and program.strip() else None
    return _scoped_group_options(db, current_user, programs=programs, search=search)


@router.get("/bluebook/groups")
def list_bluebook_groups(
    search: Optional[str] = Query(
        None,
        description="Free text matched against group name AND teacher name.",
    ),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """SAT groups the caller may open a Bluebook grid for.

    Bluebook is a College Board SAT product - NUET students do not sit it - so only SAT
    groups are listed. Scope resolution is shared with the exam-results picker.
    """
    _require(current_user, _READ_ROLES)
    return _scoped_group_options(db, current_user, programs=["sat"], search=search)


@router.get("/bluebook/groups/{group_id}/grid")
def get_bluebook_grid(
    group_id: int,
    cohort_date: Optional[date] = Query(
        None,
        description="Compare against the official result for this exact administration "
                    "instead of each student's latest.",
    ),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """Students x chronological Bluebook results for one group."""
    _require(current_user, _READ_ROLES)
    if not can_view_group(current_user, db, group_id):
        raise HTTPException(status_code=403, detail="Access denied for this group")

    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    return exam_services.build_bluebook_grid(db, group, cohort_date=cohort_date)


@router.get("/bluebook/groups/{group_id}/export")
def export_bluebook_grid(
    group_id: int,
    cohort_date: Optional[date] = None,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """XLSX of the group grid, under the same authorization as the screen."""
    _require(current_user, _READ_ROLES)
    if not can_view_group(current_user, db, group_id):
        raise HTTPException(status_code=403, detail="Access denied for this group")

    group = db.query(Group).filter(Group.id == group_id).first()
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    grid = exam_services.build_bluebook_grid(db, group, cohort_date=cohort_date)
    buffer = build_bluebook_grid_workbook(grid)
    safe_group = "".join(c for c in (group.name or "group") if c.isalnum() or c in "-_ ").strip()
    filename = f"bluebook_{safe_group or 'group'}_{date.today().isoformat()}.xlsx"
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
