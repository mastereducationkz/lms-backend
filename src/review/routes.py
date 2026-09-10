"""GET /review/quizzes and GET /review/session — read-only, teacher-facing."""
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.auth.models import UserInDB
from src.config import get_db
from src.courses.models import Lesson, Step
from src.review import service
from src.review.schemas import ReviewQuizzesResponse, ReviewSessionResponse
from src.routes.auth import get_current_user_dependency
from src.utils.permissions import check_course_access

review_router = APIRouter()


def _require_reviewer(user: UserInDB) -> None:
    if user.role not in service.REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="Access denied")


def _display_name(user: UserInDB) -> str:
    return user.official_full_name or user.name


@review_router.get("/quizzes", response_model=ReviewQuizzesResponse)
def get_review_quizzes(
    course_id: int = Query(...),
    group_id: int = Query(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Units in the course that hold quizzes, with this group's submission counts."""
    _require_reviewer(current_user)
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    service.assert_group_visible(group_id, current_user, db)
    return service.quiz_units_for_course(db, course_id, group_id)


@review_router.get("/session", response_model=ReviewSessionResponse)
def get_review_session(
    step_id: int = Query(...),
    group_id: int = Query(...),
    current_user: UserInDB = Depends(get_current_user_dependency),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Everything the presenter needs for one quiz and one group, in one request."""
    _require_reviewer(current_user)

    step = db.query(Step).filter(Step.id == step_id).first()
    if step is None or step.content_type != "quiz":
        raise HTTPException(status_code=404, detail="Quiz step not found")

    course_id = service.course_id_for_step(db, step)
    if not check_course_access(course_id, current_user, db):
        raise HTTPException(status_code=403, detail="Access denied to this course")
    service.assert_group_visible(group_id, current_user, db)

    lesson = db.query(Lesson).filter(Lesson.id == step.lesson_id).first()
    roster = service.roster_for_group(db, group_id)
    attempts = service.best_attempts_for_step(db, step.id, [u.id for u in roster])
    submitted_ids = {a.user_id for a in attempts}

    return {
        "step": {
            "step_id": step.id,
            "title": step.title,
            "lesson_id": step.lesson_id,
            "lesson_title": lesson.title if lesson else "",
            "course_id": course_id,
            "content": service.parse_quiz_content(step.content_text),
        },
        "roster": [
            {"student_id": u.id, "full_name": _display_name(u)} for u in roster
        ],
        "attempts": [
            {
                "student_id": a.user_id,
                "attempt_id": a.id,
                "correct_answers": a.correct_answers,
                "total_questions": a.total_questions,
                "score_percentage": a.score_percentage,
                "time_spent_seconds": a.time_spent_seconds,
                "completed_at": a.completed_at or a.created_at,
                "answers": a.answers,
            }
            for a in attempts
        ],
        "not_submitted": [
            {"student_id": u.id, "full_name": _display_name(u)}
            for u in roster
            if u.id not in submitted_ids
        ],
    }
