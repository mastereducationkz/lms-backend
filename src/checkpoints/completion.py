"""The one definition of "unit (lesson) completed by a student".

Mirrors how the course view derives `is_completed` (courses.py) and what the homework unit-gate
uses: an explicit lesson-level StudentProgress row marked "completed", OR every non-optional step
of the lesson completed (students usually finish step-by-step and the lesson row is never written).
"""
from typing import Dict, Iterable, List, Set, Tuple

from sqlalchemy.orm import Session

from src.courses.models import Step
from src.progress.models import StepProgress, StudentProgress


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
