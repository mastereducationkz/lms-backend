"""SAT Checkpoints domain logic (see docs/superpowers/plans/2026-09-03-sat-checkpoints.md).

Trigger rule (ТЗ §4-§6, §10): a checkpoint opens for a student when ALL required units of its
block are completed by that student — nothing else (no calendar week, no lesson counts, no
attendance). Deadline = opened_at + 24h (§8). Rows are per (student, group, checkpoint).
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.checkpoints.completion import completed_lesson_ids
from src.checkpoints.models import (
    OPEN_STATUSES, STATUS_AVAILABLE, STATUS_COMPLETED, STATUS_LOCKED, STATUS_OVERDUE,
    STATUS_REOPENED, CheckpointDefinition, StudentCheckpoint,
)
from src.courses.models import Group, GroupStudent, Lesson, Module, Step
from src.progress.models import QuizAttempt
from src.utils.permissions import get_group_course_ids

DEADLINE_HOURS = 24


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


# ---------------------------------------------------------------- lookups

def enabled_groups_for_student(db: Session, student_id: int) -> List[Group]:
    return (
        db.query(Group)
        .join(GroupStudent, GroupStudent.group_id == Group.id)
        .filter(GroupStudent.student_id == student_id,
                Group.checkpoints_enabled == True,  # noqa: E712
                Group.is_active == True)  # noqa: E712
        .order_by(Group.id)
        .all()
    )


def definitions_for_group(db: Session, group: Group, *, only_active: bool = True) -> List[CheckpointDefinition]:
    course_ids = get_group_course_ids(db, group.id)
    if not course_ids:
        return []
    q = db.query(CheckpointDefinition).filter(CheckpointDefinition.course_id.in_(course_ids))
    if only_active:
        q = q.filter(CheckpointDefinition.is_active == True)  # noqa: E712
    return q.order_by(CheckpointDefinition.number).all()


def get_row(db: Session, student_id: int, group_id: int, checkpoint_id: int) -> Optional[StudentCheckpoint]:
    return db.query(StudentCheckpoint).filter(
        StudentCheckpoint.student_id == student_id,
        StudentCheckpoint.group_id == group_id,
        StudentCheckpoint.checkpoint_id == checkpoint_id,
    ).first()


def unit_progress(db: Session, student_id: int, definition: CheckpointDefinition) -> List[Dict[str, Any]]:
    """Per required unit: completed or not (ТЗ §13 'which required units are done')."""
    units = list(definition.required_units)
    done = completed_lesson_ids(db, student_id, [u.lesson_id for u in units])
    titles = dict(db.query(Lesson.id, Lesson.title).filter(
        Lesson.id.in_([u.lesson_id for u in units])).all()) if units else {}
    return [{
        "lesson_id": u.lesson_id,
        "title": titles.get(u.lesson_id, ""),
        "kind": u.kind,
        "completed": u.lesson_id in done,
    } for u in units]


def locked_reason(units: List[Dict[str, Any]]) -> Optional[str]:
    missing = [u["title"] or f"lesson {u['lesson_id']}" for u in units if not u["completed"]]
    if not missing:
        return None
    return "Locked — waiting for " + ", ".join(missing)


# ---------------------------------------------------------------- auto-open

def _open_row(row: Optional[StudentCheckpoint], *, student_id: int, group_id: int,
              definition: CheckpointDefinition, now: datetime, opened_by: str,
              deadline: Optional[datetime], actor_id: Optional[int]) -> StudentCheckpoint:
    if row is None:
        row = StudentCheckpoint(student_id=student_id, group_id=group_id,
                                checkpoint_id=definition.id, checkpoint_number=definition.number)
    row.checkpoint_number = definition.number
    row.required_unit_ids = [u.lesson_id for u in definition.required_units]
    row.status = STATUS_AVAILABLE
    row.opened_at = now
    row.deadline = deadline or (now + timedelta(hours=DEADLINE_HOURS))
    row.opened_by = opened_by
    row.updated_by = actor_id
    return row


def sync_student_checkpoints(db: Session, student_id: int, *, now: Optional[datetime] = None,
                             commit: bool = True) -> List[StudentCheckpoint]:
    """Open every checkpoint whose required units the student has completed, in every enabled
    group. Idempotent: rows that are already open/completed/overdue/reopened are left alone."""
    now = now or utcnow()
    opened: List[StudentCheckpoint] = []
    for group in enabled_groups_for_student(db, student_id):
        for definition in definitions_for_group(db, group):
            if definition.number < (group.checkpoints_start_number or 1):
                continue
            required = [u.lesson_id for u in definition.required_units]
            if not required:
                continue
            row = get_row(db, student_id, group.id, definition.id)
            if row is not None and row.status != STATUS_LOCKED:
                continue
            if set(required) <= completed_lesson_ids(db, student_id, required):
                row = _open_row(row, student_id=student_id, group_id=group.id, definition=definition,
                                now=now, opened_by="auto", deadline=None, actor_id=None)
                db.add(row)
                opened.append(row)
    if opened:
        db.flush()
        if commit:
            db.commit()
    return opened


def sync_group(db: Session, group: Group, *, now: Optional[datetime] = None, commit: bool = True) -> int:
    """Run sync for every student of a group (used when a group is enabled). Returns rows opened."""
    student_ids = [r[0] for r in db.query(GroupStudent.student_id).filter(GroupStudent.group_id == group.id).all()]
    total = 0
    for sid in student_ids:
        total += len(sync_student_checkpoints(db, sid, now=now, commit=False))
    if commit:
        db.commit()
    return total


# ---------------------------------------------------------------- overdue

def refresh_overdue(rows: Iterable[StudentCheckpoint], now: Optional[datetime] = None) -> List[StudentCheckpoint]:
    now = now or utcnow()
    flipped = []
    for row in rows:
        if row.status in OPEN_STATUSES and row.deadline is not None and naive(row.deadline) < now:
            row.status = STATUS_OVERDUE
            flipped.append(row)
    return flipped
