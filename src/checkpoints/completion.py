"""The one definition of "unit (lesson) completed by a student".

Mirrors how the course view derives `is_completed` (courses.py) and what the homework unit-gate
uses: an explicit lesson-level StudentProgress row marked "completed", OR every non-optional step
of the lesson completed (students usually finish step-by-step and the lesson row is never written).
"""
from typing import Dict, Iterable, List, Set, Tuple

from sqlalchemy.orm import Session

from src.courses.models import Step
from src.progress.models import StepProgress, StudentProgress


def _required_steps_by_lesson(
    db: Session, lesson_ids: List[int]
) -> Dict[int, List[int]]:
    """Per lesson: non-optional step ids, or every step id if all steps are optional."""
    steps_by_lesson: Dict[int, List[Tuple[int, bool]]] = {}
    for sid, lid, is_optional in db.query(Step.id, Step.lesson_id, Step.is_optional).filter(
        Step.lesson_id.in_(lesson_ids)
    ).all():
        steps_by_lesson.setdefault(lid, []).append((sid, bool(is_optional)))
    required: Dict[int, List[int]] = {}
    for lid, lesson_steps in steps_by_lesson.items():
        required_steps = [s for s in lesson_steps if not s[1]] or lesson_steps
        required[lid] = [sid for sid, _ in required_steps]
    return required


def completed_lesson_ids(db: Session, user_id: int, lesson_ids: Iterable[int]) -> Set[int]:
    lesson_ids = list(dict.fromkeys(int(x) for x in lesson_ids))
    if not lesson_ids:
        return set()

    completed: Set[int] = {
        row[0] for row in db.query(StudentProgress.lesson_id).filter(
            StudentProgress.user_id == user_id,
            StudentProgress.lesson_id.in_(lesson_ids),
            StudentProgress.status == "completed",
        ).all()
    }
    remaining = [lid for lid in lesson_ids if lid not in completed]
    if not remaining:
        return completed

    completed_step_ids: Set[int] = {
        row[0] for row in db.query(StepProgress.step_id).filter(
            StepProgress.user_id == user_id,
            StepProgress.lesson_id.in_(remaining),
            StepProgress.status == "completed",
        ).all()
    }
    steps_by_lesson: Dict[int, List[Tuple[int, bool]]] = {}
    for sid, lid, is_optional in db.query(Step.id, Step.lesson_id, Step.is_optional).filter(
        Step.lesson_id.in_(remaining)
    ).all():
        steps_by_lesson.setdefault(lid, []).append((sid, bool(is_optional)))
    for lid in remaining:
        lesson_steps = steps_by_lesson.get(lid, [])
        required = [s for s in lesson_steps if not s[1]] or lesson_steps
        if required and all(sid in completed_step_ids for sid, _ in required):
            completed.add(lid)
    return completed


def completed_lesson_counts(
    db: Session, user_ids: Iterable[int], lesson_ids: Iterable[int]
) -> Dict[int, int]:
    """Bulk sibling of `completed_lesson_ids`: {lesson_id: how many of user_ids completed it}.

    Same two rules (explicit StudentProgress "completed" row, OR every required step
    completed) and the same optional-step handling, but set-based across the whole roster
    in a fixed handful of queries instead of one call to `completed_lesson_ids` per user —
    that would be ~3 queries x N students on an endpoint like /review/quizzes.

    Lessons with zero completions are simply absent from the result (callers already do
    `counts.get(lesson_id, 0)`, matching `submitted_count` on the same endpoint).
    """
    user_ids = list(dict.fromkeys(int(x) for x in user_ids))
    lesson_ids = list(dict.fromkeys(int(x) for x in lesson_ids))
    if not user_ids or not lesson_ids:
        return {}

    # Explicit lesson-level completions: (user_id, lesson_id) pairs.
    completed_pairs: Set[Tuple[int, int]] = {
        (uid, lid) for uid, lid in db.query(
            StudentProgress.user_id, StudentProgress.lesson_id
        ).filter(
            StudentProgress.user_id.in_(user_ids),
            StudentProgress.lesson_id.in_(lesson_ids),
            StudentProgress.status == "completed",
        ).all()
    }

    # Step-based completions, for whichever (user, lesson) pairs aren't already explicit.
    required_by_lesson = _required_steps_by_lesson(db, lesson_ids)
    all_required_step_ids = sorted({sid for steps in required_by_lesson.values() for sid in steps})
    completed_steps_by_user: Dict[int, Set[int]] = {}
    if all_required_step_ids:
        for uid, sid in db.query(StepProgress.user_id, StepProgress.step_id).filter(
            StepProgress.user_id.in_(user_ids),
            StepProgress.step_id.in_(all_required_step_ids),
            StepProgress.status == "completed",
        ).all():
            completed_steps_by_user.setdefault(uid, set()).add(sid)

    for lid, required in required_by_lesson.items():
        if not required:
            continue
        required_set = set(required)
        for uid in user_ids:
            if (uid, lid) in completed_pairs:
                continue
            if required_set <= completed_steps_by_user.get(uid, set()):
                completed_pairs.add((uid, lid))

    counts: Dict[int, int] = {}
    for _uid, lid in completed_pairs:
        counts[lid] = counts.get(lid, 0) + 1
    return counts
