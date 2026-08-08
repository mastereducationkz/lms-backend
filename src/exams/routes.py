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
from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from src.assignments.exam_dates import (
    SAT_DATES_SOURCE_URL,
    SAT_DATES_VERIFIED_AT,
    SAT_TEST_DATES,
    get_nearest_sat_date,
)
from src.auth.models import UserInDB
from src.config import get_db
from src.courses.models import Group, GroupStudent
from src.exams.models import ExamResult
from src.exams.schemas import (
    ExamResultCreate,
    ExamResultOut,
    ExamResultRow,
    ExamResultUpdate,
)
from src.exams import services as exam_services
from src.exams.exports import build_bluebook_grid_workbook, build_exam_results_workbook
from src.routes.auth import get_current_user_dependency
from src.utils.scope import UNRESTRICTED, can_view_group, visible_group_ids

router = APIRouter()

# Who may READ exam data, subject to row scope.
_READ_ROLES = {"teacher", "curator", "head_teacher", "head_curator", "admin"}
# Who may CREATE or CORRECT an official result. Teachers are read-only here:
# an official exam score is a record of fact, owned by the curator team.
_WRITE_ROLES = {"curator", "head_curator", "admin"}


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

    latest: dict = {}
    for r in result_query.order_by(ExamResult.test_date.asc()).all():
        latest[r.student_id] = r

    group_names = _group_names_for_students(db, ids)

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
        rows.append(ExamResultRow(
            student=contact,
            group_id=group_id_value,
            group_name=group_name,
            planned_test_date=planned_date,
            result=(
                ExamResultOut.model_validate(exam_services._with_proof_flag(result))
                if result else None
            ),
        ))
    return rows


def _group_names_for_students(db: Session, student_ids: List[int]) -> dict:
    """First live group per student, for display. Batched."""
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
    out = {}
    for student_id, gid, gname in rows:
        out.setdefault(student_id, (gid, gname))
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
    db.commit()
    db.refresh(result)
    return ExamResultOut.model_validate(exam_services._with_proof_flag(result))


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
