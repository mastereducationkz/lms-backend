"""Review mode reads: rosters, best attempts, and who is allowed to see which group.

Deliberately dumb about answers: `quiz_attempts.answers` is passed through as the
string it was stored as. Correctness is decided in the frontend by `gradeQuestion`,
the same function that produced the student's own score — a second grader here
would be a second answer key, free to drift from the first.
"""
import json
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent, Lesson, Module, Step
from src.progress.models import QuizAttempt

# Roles allowed into review mode at all. Group narrowing happens in visible_group_ids.
REVIEWER_ROLES = ("teacher", "curator", "admin", "head_curator", "head_teacher")

# Everyone-sees-everything roles.
UNRESTRICTED_ROLES = ("admin", "head_curator", "head_teacher")


def parse_quiz_content(content_text: Optional[str]) -> Dict[str, Any]:
    """The step's content JSON, or {} for anything unparseable."""
    if not content_text:
        return {}
    try:
        data = json.loads(content_text)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def quiz_questions(content_text: Optional[str]) -> List[Dict[str, Any]]:
    """Real questions only — `image_content` blocks are layout, not questions."""
    questions = parse_quiz_content(content_text).get("questions")
    if not isinstance(questions, list):
        return []
    return [
        q for q in questions
        if isinstance(q, dict) and q.get("question_type") != "image_content"
    ]


def quiz_question_count(content_text: Optional[str]) -> int:
    return len(quiz_questions(content_text))


def visible_group_ids(user: UserInDB, db: Session) -> Optional[List[int]]:
    """Group ids this user may review. None means "no restriction"."""
    if user.role in UNRESTRICTED_ROLES:
        return None
    if user.role == "teacher":
        rows = db.query(Group.id).filter(Group.teacher_id == user.id).all()
    elif user.role == "curator":
        rows = db.query(Group.id).filter(
            Group.curator_id == user.id,
            Group.is_active.is_(True),
            Group.is_over.is_(False),
        ).all()
    else:
        raise HTTPException(status_code=403, detail="Access denied")
    return [row[0] for row in rows]


def assert_group_visible(group_id: int, user: UserInDB, db: Session) -> None:
    allowed = visible_group_ids(user, db)
    if allowed is not None and group_id not in allowed:
        raise HTTPException(status_code=403, detail="You don't have access to this group")


def roster_for_group(db: Session, group_id: int) -> List[UserInDB]:
    """Active students in the group, by name."""
    return (
        db.query(UserInDB)
        .join(GroupStudent, GroupStudent.student_id == UserInDB.id)
        .filter(GroupStudent.group_id == group_id, UserInDB.is_active.is_(True))
        .order_by(UserInDB.name)
        .all()
    )


def best_attempts_for_step(
    db: Session, step_id: int, student_ids: Sequence[int]
) -> List[QuizAttempt]:
    """One row per student: highest score, ties broken by the later attempt.

    DISTINCT ON is Postgres-specific and is the idiom already used in
    src/admin/routes/analytics.py for exactly this shape of query.
    """
    if not student_ids:
        return []
    done_at = func.coalesce(QuizAttempt.completed_at, QuizAttempt.created_at)
    return (
        db.query(QuizAttempt)
        .filter(
            QuizAttempt.step_id == step_id,
            QuizAttempt.is_draft.is_(False),
            QuizAttempt.user_id.in_(list(student_ids)),
        )
        .distinct(QuizAttempt.user_id)
        .order_by(
            QuizAttempt.user_id,
            QuizAttempt.score_percentage.desc(),
            done_at.desc(),
        )
        .all()
    )


def course_id_for_step(db: Session, step: Step) -> int:
    """The course a quiz step belongs to (steps reach it via lesson -> module)."""
    row = (
        db.query(Module.course_id)
        .join(Lesson, Lesson.module_id == Module.id)
        .filter(Lesson.id == step.lesson_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Quiz step not found")
    return row[0]


def quiz_units_for_course(db: Session, course_id: int, group_id: int) -> Dict[str, Any]:
    """Units in the course that hold quiz steps, with how many of the group submitted."""
    roster_ids = [u.id for u in roster_for_group(db, group_id)]

    rows = (
        db.query(Lesson, Step)
        .join(Module, Lesson.module_id == Module.id)
        .join(Step, Step.lesson_id == Lesson.id)
        .filter(
            Module.course_id == course_id,
            Lesson.kind == "unit",
            Step.content_type == "quiz",
        )
        .order_by(Module.order_index, Lesson.order_index, Step.order_index)
        .all()
    )

    submitted: Dict[int, int] = {}
    step_ids = [step.id for _, step in rows]
    if step_ids and roster_ids:
        counts = (
            db.query(QuizAttempt.step_id, func.count(func.distinct(QuizAttempt.user_id)))
            .filter(
                QuizAttempt.step_id.in_(step_ids),
                QuizAttempt.is_draft.is_(False),
                QuizAttempt.user_id.in_(roster_ids),
            )
            .group_by(QuizAttempt.step_id)
            .all()
        )
        submitted = {step_id: count for step_id, count in counts}

    units: List[Dict[str, Any]] = []
    by_lesson: Dict[int, Dict[str, Any]] = {}
    for lesson, step in rows:
        unit = by_lesson.get(lesson.id)
        if unit is None:
            unit = {
                "lesson_id": lesson.id,
                "title": lesson.title,
                "order": lesson.order_index,
                "quizzes": [],
            }
            by_lesson[lesson.id] = unit
            units.append(unit)
        unit["quizzes"].append({
            "step_id": step.id,
            "title": step.title,
            "question_count": quiz_question_count(step.content_text),
            "submitted_count": submitted.get(step.id, 0),
        })

    return {
        "course_id": course_id,
        "group_id": group_id,
        "roster_count": len(roster_ids),
        "units": units,
    }
