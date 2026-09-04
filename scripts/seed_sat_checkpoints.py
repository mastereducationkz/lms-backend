"""Seed SAT Checkpoints: a "Checkpoints" module inside the SAT course + 9 CheckpointDefinitions.

    python -m scripts.seed_sat_checkpoints --course-id 1 [--blocks 9] [--dry-run]

Idempotent: re-running updates nothing that an admin may have edited (required units, is_active,
quiz lesson) — it only creates what is missing. If lessons from an earlier, separate-course seed
(titled "SAT Checkpoints") are still referenced by this course's definitions, they are re-parented
into the module in place (see `_move_legacy_lessons`) rather than duplicated.
"""
import argparse
import json
from typing import Any, Dict, List

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.checkpoints.models import CheckpointDefinition, CheckpointRequiredUnit, DEFAULT_TOTAL_QUESTIONS
from src.courses.models import Course, Lesson, Module, Step

MODULE_TITLE = "Checkpoints"
LEGACY_COURSE_TITLE = "SAT Checkpoints"


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


def _ensure_module(db: Session, sat_course_id: int, title: str) -> Module:
    """The checkpoints module lives in the SAT course itself, after Verbal and Math."""
    module = db.query(Module).filter(
        Module.course_id == sat_course_id, Module.title == title
    ).first()
    if module is None:
        highest = db.query(func.max(Module.order_index)).filter(
            Module.course_id == sat_course_id
        ).scalar()
        module = Module(title=title, course_id=sat_course_id,
                        order_index=(highest + 1) if highest is not None else 0,
                        description="Block checkpoints. Each opens once its 2 Verbal units and "
                                    "1 Math unit are completed.")
        db.add(module)
        db.flush()
    return module


def _move_legacy_lessons(db: Session, sat_course_id: int, module: Module) -> int:
    """Re-parent quiz lessons created by an earlier seed into the SAT course's module.

    Keeps lesson ids, steps and any student rows intact — only the parent module changes.
    """
    definitions = db.query(CheckpointDefinition).filter(
        CheckpointDefinition.course_id == sat_course_id,
        CheckpointDefinition.quiz_lesson_id.isnot(None),
    ).all()
    moved = 0
    for definition in definitions:
        lesson = db.get(Lesson, definition.quiz_lesson_id)
        if lesson is not None and (lesson.module_id != module.id or lesson.kind != "checkpoint"):
            lesson.module_id = module.id
            lesson.order_index = definition.number - 1
            lesson.kind = "checkpoint"
            moved += 1
    if moved:
        db.flush()
    return moved


def _ensure_quiz_lesson(db: Session, module: Module, number: int) -> Lesson:
    title = f"Checkpoint {number}"
    lesson = db.query(Lesson).filter(
        Lesson.module_id == module.id, Lesson.title == title
    ).first()
    if lesson is None:
        lesson = Lesson(title=title, module_id=module.id, order_index=number - 1,
                        is_initially_unlocked=True, kind="checkpoint",
                        description="45 questions: 2 Verbal units + 1 Math unit, "
                                    "5 easy / 5 medium / 5 hard each.")
        db.add(lesson)
        db.flush()
        db.add(Step(lesson_id=lesson.id, title="Quiz", content_type="quiz", order_index=0,
                    content_text=json.dumps({"title": title, "questions": []})))
        db.flush()
    lesson.kind = "checkpoint"
    db.flush()
    return lesson


def seed(db: Session, *, sat_course_id: int, blocks: int = 9, module_title: str = MODULE_TITLE,
         dry_run: bool = False) -> Dict[str, Any]:
    if db.get(Course, sat_course_id) is None:
        raise SystemExit(f"Course {sat_course_id} not found")
    mapping = default_mapping(db, sat_course_id, blocks)
    if dry_run:
        return {"module_id": None, "created_definitions": 0, "updated_definitions": 0,
                "moved_lessons": 0, "mapping": mapping}

    module = _ensure_module(db, sat_course_id, module_title)
    moved = _move_legacy_lessons(db, sat_course_id, module)
    created = 0
    updated = 0
    for n, (v1, v2, m1) in enumerate(mapping, start=1):
        lesson = _ensure_quiz_lesson(db, module, n)
        definition = db.query(CheckpointDefinition).filter_by(course_id=sat_course_id, number=n).first()
        if definition is not None:
            if definition.quiz_lesson_id is None:
                definition.quiz_lesson_id = lesson.id
                updated += 1
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
    return {"module_id": module.id, "created_definitions": created, "updated_definitions": updated,
            "moved_lessons": moved, "mapping": mapping}


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
