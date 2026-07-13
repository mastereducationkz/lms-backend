"""Admin endpoint: Weekly Top Students composite leaderboard.

Ranks students for an Almaty ISO week by a single Activity Score combining
homework, course progress, study time, and points/streak. See
``src.services.weekly_top_students_service`` for the scoring methodology.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.config import get_db
from src.schemas.models import UserInDB
from src.utils.permissions import require_admin
from src.services.cache_service import cached
from src.services.excel_export_service import get_excel_export_service
from src.services.weekly_top_students_service import compute_weekly_top_students

router = APIRouter()


# --------------------------------------------------------------------------- #
# Response schemas
# --------------------------------------------------------------------------- #

class HomeworkBreakdown(BaseModel):
    due: int
    submitted: int
    on_time: int
    late: int
    missing: int
    avg_pct: Optional[float] = None
    subscore: Optional[float] = None


class SubScores(BaseModel):
    homework: Optional[float] = None
    course: float
    study: float
    engagement: float


class WeeklyTopStudentRow(BaseModel):
    rank: int
    student_id: int
    student_name: str
    group_name: Optional[str] = None
    teacher_name: str
    curator_name: str
    program_type: Optional[str] = None
    homework: HomeworkBreakdown
    steps_week: int
    top_course_name: Optional[str] = None
    course_pct_week: float
    course_pct_total: float
    study_minutes_week: int
    points_week: int
    daily_streak: int
    daily_questions_week: int
    subscores: SubScores
    activity_score: float


class NeedsAttentionRow(BaseModel):
    student_id: int
    student_name: str
    group_name: Optional[str] = None
    teacher_name: str
    curator_name: str
    due: int
    submitted: int
    on_time: int


class WeeklyTopStudentsResponse(BaseModel):
    week_start: str
    week_end: str
    generated_at: str
    total_students: int
    limit: int
    weights: dict
    students: List[WeeklyTopStudentRow]
    needs_attention: List[NeedsAttentionRow]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.get("/weekly-top-students", response_model=WeeklyTopStudentsResponse)
@cached(
    namespace="admin:weekly-top-students",
    ttl=300,
    key_args=("week_start", "program_type", "group_id", "curator_id", "teacher_id", "limit"),
)
def get_weekly_top_students(
    week_start: Optional[date] = Query(
        None, description="Any date in the target week; snapped to Almaty Monday. Defaults to current week."
    ),
    program_type: Optional[str] = Query(None, description="Filter by group program_type (e.g. sat, ielts)."),
    group_id: Optional[int] = Query(None),
    curator_id: Optional[int] = Query(None),
    teacher_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=1000, description="Top-N count to return."),
    current_user: UserInDB = Depends(require_admin()),
    db: Session = Depends(get_db),
):
    return compute_weekly_top_students(
        db,
        week_start,
        program_type=program_type,
        group_id=group_id,
        curator_id=curator_id,
        teacher_id=teacher_id,
        limit=limit,
    )


@router.get("/weekly-top-students/export")
def export_weekly_top_students(
    week_start: Optional[date] = Query(None),
    program_type: Optional[str] = Query(None),
    group_id: Optional[int] = Query(None),
    curator_id: Optional[int] = Query(None),
    teacher_id: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    current_user: UserInDB = Depends(require_admin()),
    db: Session = Depends(get_db),
):
    data = compute_weekly_top_students(
        db,
        week_start,
        program_type=program_type,
        group_id=group_id,
        curator_id=curator_id,
        teacher_id=teacher_id,
        limit=limit,
    )
    buffer = get_excel_export_service().create_weekly_top_students_workbook(data)
    filename = f"weekly-top-students_{data['week_start']}.xlsx"
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
