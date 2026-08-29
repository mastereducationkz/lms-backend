"""Staff-facing student results report (mounted at /reports).

Access matrix (a deliberate product decision, narrower than check_student_access):

- admin, head_curator, head_teacher: any student
- curator: only students in groups they curate
- everyone else (teachers, students, parents): no access

Regular teachers are intentionally excluded — the report bundles cross-course data
(attendance, activity, other courses' results) beyond a single teacher's scope.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from src.config import get_db
from src.routes.auth import get_current_user_dependency
from src.schemas.models import UserInDB
from src.utils.permissions import check_student_access
from src.reports.services import build_student_report, build_submission_detail
from src.reports.external import fetch_weekly_tests
from src.reports.pdf import render_student_report_pdf

router = APIRouter()

_FULL_ACCESS_ROLES = {"admin", "head_curator", "head_teacher"}


def _require_report_access(student_id: int, user: UserInDB, db: Session) -> None:
    if user.role in _FULL_ACCESS_ROLES:
        return
    # check_student_access scopes curators to their own groups.
    if user.role == "curator" and check_student_access(student_id, user, db):
        return
    raise HTTPException(status_code=403, detail="Not authorized to view student reports")


async def _full_report(db: Session, student_id: int) -> dict:
    """LMS-resident sections plus weekly tests from the external exam platforms."""
    report = build_student_report(db, student_id)
    student = db.query(UserInDB).filter(UserInDB.id == student_id).first()
    report["weekly_tests"] = await fetch_weekly_tests(db, student)
    return report


@router.get("/students/{student_id}")
async def student_report(
    student_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """The full results report as JSON (drives the report page in the UI)."""
    _require_report_access(student_id, current_user, db)
    return await _full_report(db, student_id)


@router.get("/students/{student_id}/pdf")
async def student_report_pdf(
    student_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """The same report rendered as a downloadable PDF."""
    _require_report_access(student_id, current_user, db)
    report = await _full_report(db, student_id)
    buffer = render_student_report_pdf(report)
    # ASCII-only filename: student names are Cyrillic and Content-Disposition
    # header values must stay latin-1 safe.
    filename = f"student_{student_id}_report.pdf"
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/students/{student_id}/submissions/{submission_id}")
def student_submission_detail(
    student_id: int,
    submission_id: int,
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
):
    """A homework submission's content, for the report page drill-down."""
    _require_report_access(student_id, current_user, db)
    return build_submission_detail(db, student_id, submission_id)
