"""Seed SAT Checkpoints: hidden quiz course + 9 CheckpointDefinitions for a SAT course.

    python -m scripts.seed_sat_checkpoints --course-id 1 [--blocks 9] [--dry-run]

Idempotent: re-running updates nothing that an admin may have edited (required units, is_active,
quiz lesson) — it only creates what is missing. The hidden course has NO CourseGroupAccess rows,
so students reach it only through an opened checkpoint (see check_course_access hook).
"""
import argparse
import json
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from src.checkpoints.models import CheckpointDefinition, CheckpointRequiredUnit, DEFAULT_TOTAL_QUESTIONS
from src.courses.models import Course, Lesson, Module, Step

QUIZ_COURSE_TITLE = "SAT Checkpoints"


def _unit_lessons(db: Session, course_id: int, module_title: str) -> List[Lesson]:
    module = db.query(Module).filter(Module.course_id == course_id, Module.title.ilike(module_title)).first()
    if module is None:
        raise SystemExit(f"Course {course_id} has no module titled {module_title!r}")
    lessons = db.query(Lesson).filter(Lesson.module_id == module.id).order_by(Lesson.order_index, Lesson.id).all()
    return [l for l in lessons if not l.title.strip().lower().startswith("unit 0")]


def default_mapping(db: Session, sat_course_id: int, blocks: int) -> List[List[int]]:
    verbal = _unit_lessons(db, sat_course_id, "Verbal")
    math = _unit_lessons(db, sat_course_id, "Math")
    if len(verbal) < 2 * blocks or len(math) < blocks:
        raise SystemExit(f"Need {2 * blocks} verbal and {blocks} math units, found {len(verbal)} / {len(math)}")
    return [[verbal[2 * n].id, verbal[2 * n + 1].id, math[n].id] for n in range(blocks)]


def _ensure_quiz_course(db: Session, title: str) -> Course:
    course = db.query(Course).filter(Course.title == title).first()
    if course is None:
        course = Course(title=title, course_type="sat", is_active=True, release_schedule="all",
                        description="Hidden container for SAT Checkpoint quizzes. Do not grant group access.")
        db.add(course); db.flush()
        db.add(Module(title="Checkpoints", course_id=course.id, order_index=0)); db.flush()
    return course


def _ensure_quiz_lesson(db: Session, quiz_course: Course, number: int) -> Lesson:
    module = db.query(Module).filter(Module.course_id == quiz_course.id).order_by(Module.order_index).first()
    title = f"Checkpoint {number}"
    lesson = db.query(Lesson).filter(Lesson.module_id == module.id, Lesson.title == title).first()
    if lesson is None:
        lesson = Lesson(title=title, module_id=module.id, order_index=number - 1, is_initially_unlocked=True,
                        description="45 questions: 2 Verbal units + 1 Math unit, 5 easy / 5 medium / 5 hard each.")
        db.add(lesson); db.flush()
        db.add(Step(lesson_id=lesson.id, title="Quiz", content_type="quiz", order_index=0,
                    content_text=json.dumps({"title": title, "questions": []})))
        db.flush()
    return lesson


def seed(db: Session, *, sat_course_id: int, blocks: int = 9, quiz_course_title: str = QUIZ_COURSE_TITLE,
         dry_run: bool = False) -> Dict[str, Any]:
    if db.get(Course, sat_course_id) is None:
        raise SystemExit(f"Course {sat_course_id} not found")
    mapping = default_mapping(db, sat_course_id, blocks)
    if dry_run:
        return {"quiz_course_id": None, "created_definitions": 0, "updated_definitions": 0, "mapping": mapping}

    quiz_course = _ensure_quiz_course(db, quiz_course_title)
    created = 0
    for n, (v1, v2, m1) in enumerate(mapping, start=1):
        lesson = _ensure_quiz_lesson(db, quiz_course, n)
        definition = db.query(CheckpointDefinition).filter_by(course_id=sat_course_id, number=n).first()
        if definition is not None:
            continue
        definition = CheckpointDefinition(course_id=sat_course_id, number=n, title=f"Checkpoint {n}",
                                          quiz_lesson_id=lesson.id, total_questions=DEFAULT_TOTAL_QUESTIONS,
                                          is_active=False)
        db.add(definition); db.flush()
        db.add_all([
            CheckpointRequiredUnit(checkpoint_id=definition.id, lesson_id=v1, kind="verbal", position=0),
            CheckpointRequiredUnit(checkpoint_id=definition.id, lesson_id=v2, kind="verbal", position=1),
            CheckpointRequiredUnit(checkpoint_id=definition.id, lesson_id=m1, kind="math", position=2),
        ])
        created += 1
    db.commit()
    return {"quiz_course_id": quiz_course.id, "created_definitions": created, "updated_definitions": 0, "mapping": mapping}


def main() -> None:  # pragma: no cover - operator CLI
    from src.config import SessionLocal
    parser = argparse.ArgumentParser(description="Seed SAT Checkpoints")
    parser.add_argument("--course-id", type=int, required=True, help="the SAT course whose units gate checkpoints")
    parser.add_argument("--blocks", type=int, default=9)
    parser.add_argument("--dry-run", action="store_true", help="print the unit mapping and exit")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        out = seed(db, sat_course_id=args.course_id, blocks=args.blocks, dry_run=args.dry_run)
        titles = dict(db.query(Lesson.id, Lesson.title).filter(
            Lesson.id.in_([lid for block in out["mapping"] for lid in block])).all())
        for n, block in enumerate(out["mapping"], start=1):
            print(f"Checkpoint {n}: " + " | ".join(f"{lid} {titles.get(lid, '?')}" for lid in block))
        print({k: v for k, v in out.items() if k != "mapping"})
    finally:
        db.close()


if __name__ == "__main__":
    main()
