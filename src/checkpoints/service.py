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


# ---------------------------------------------------------------- admin actions

def _target_student_ids(db: Session, group: Group, student_ids: Optional[List[int]]) -> List[int]:
    members = [r[0] for r in db.query(GroupStudent.student_id).filter(GroupStudent.group_id == group.id).all()]
    if student_ids is None:
        return members
    allowed = set(members)
    bad = [sid for sid in student_ids if sid not in allowed]
    if bad:
        raise HTTPException(status_code=400, detail=f"Students not in group {group.id}: {bad}")
    return list(dict.fromkeys(student_ids))


def open_for_students(db: Session, *, group: Group, definition: CheckpointDefinition,
                      student_ids: Optional[List[int]] = None, deadline: Optional[datetime] = None,
                      actor_id: int, now: Optional[datetime] = None, commit: bool = True) -> List[StudentCheckpoint]:
    """Manual open (ТЗ §13): locked/missing rows become available. Open rows are left alone."""
    now = now or utcnow()
    deadline = naive(deadline)
    changed = []
    for sid in _target_student_ids(db, group, student_ids):
        row = get_row(db, sid, group.id, definition.id)
        if row is not None and row.status != STATUS_LOCKED:
            continue
        row = _open_row(row, student_id=sid, group_id=group.id, definition=definition, now=now,
                        opened_by="admin", deadline=deadline, actor_id=actor_id)
        db.add(row)
        changed.append(row)
    db.flush()
    if commit:
        db.commit()
    return changed


def reopen_for_students(db: Session, *, group: Group, definition: CheckpointDefinition,
                        student_ids: Optional[List[int]] = None, deadline: Optional[datetime] = None,
                        actor_id: int, now: Optional[datetime] = None, commit: bool = True) -> List[StudentCheckpoint]:
    """Reopen (ТЗ §13) for one student or the whole group: new deadline, status 'reopened'.
    The previous result stays on the row until a new submission overwrites it."""
    now = now or utcnow()
    deadline = naive(deadline) or (now + timedelta(hours=DEADLINE_HOURS))
    changed = []
    for sid in _target_student_ids(db, group, student_ids):
        row = get_row(db, sid, group.id, definition.id)
        if row is None:
            row = _open_row(None, student_id=sid, group_id=group.id, definition=definition, now=now,
                            opened_by="admin", deadline=deadline, actor_id=actor_id)
        row.status = STATUS_REOPENED
        row.deadline = deadline
        row.reopen_count = (row.reopen_count or 0) + 1
        row.updated_by = actor_id
        if row.opened_at is None:
            row.opened_at = now
        db.add(row)
        changed.append(row)
    db.flush()
    if commit:
        db.commit()
    return changed


def set_deadline(db: Session, row: StudentCheckpoint, deadline: datetime, actor_id: int,
                 now: Optional[datetime] = None, commit: bool = True) -> StudentCheckpoint:
    now = now or utcnow()
    row.deadline = naive(deadline)
    row.updated_by = actor_id
    if row.status == STATUS_OVERDUE and row.deadline > now:
        row.status = STATUS_AVAILABLE
    db.flush()
    if commit:
        db.commit()
    return row


# ---------------------------------------------------------------- quiz gate + recording

def checkpoint_definition_for_step(db: Session, step_id: int) -> Optional[CheckpointDefinition]:
    lesson_id = db.query(Step.lesson_id).filter(Step.id == step_id).scalar()
    if lesson_id is None:
        return None
    return db.query(CheckpointDefinition).filter(CheckpointDefinition.quiz_lesson_id == lesson_id).first()


def _rows_for_student_definition(db: Session, student_id: int, definition: CheckpointDefinition) -> List[StudentCheckpoint]:
    return db.query(StudentCheckpoint).filter(
        StudentCheckpoint.student_id == student_id,
        StudentCheckpoint.checkpoint_id == definition.id,
    ).order_by(StudentCheckpoint.id).all()


def assert_can_submit(db: Session, student_id: int, definition: CheckpointDefinition,
                      now: Optional[datetime] = None, *,
                      allow_past_deadline: bool = False) -> List[StudentCheckpoint]:
    """Server-side gate for POST /progress/quiz-attempt on a checkpoint quiz step.

    `allow_past_deadline` relaxes only the deadline check (used for draft autosaves) —
    a locked or completed checkpoint still rejects the submission.
    """
    now = now or utcnow()
    rows = _rows_for_student_definition(db, student_id, definition)
    if not rows or all(r.status == STATUS_LOCKED for r in rows):
        raise HTTPException(status_code=403, detail="Checkpoint is locked: complete the required units first")
    open_rows = [r for r in rows if r.status in OPEN_STATUSES]
    if not open_rows:
        if any(r.status == STATUS_COMPLETED for r in rows):
            raise HTTPException(status_code=409, detail="Checkpoint already completed")
        raise HTTPException(status_code=409, detail="Checkpoint deadline has passed; ask your admin to reopen it")
    if allow_past_deadline:
        return open_rows
    live = [r for r in open_rows if r.deadline is None or naive(r.deadline) >= now]
    if not live:
        refresh_overdue(open_rows, now)
        db.flush()
        raise HTTPException(status_code=409, detail="Checkpoint deadline has passed; ask your admin to reopen it")
    return live


def record_submission(db: Session, student_id: int, attempt: QuizAttempt,
                      now: Optional[datetime] = None, commit: bool = True) -> List[StudentCheckpoint]:
    """Copy a finalized quiz attempt into every open row of that checkpoint for the student."""
    now = now or utcnow()
    definition = checkpoint_definition_for_step(db, attempt.step_id)
    if definition is None or attempt.is_draft:
        return []
    done = []
    for row in _rows_for_student_definition(db, student_id, definition):
        # Deadline gating already happened in assert_can_submit; record onto anything not
        # already terminal (locked/completed), including a row flipped to overdue in the
        # meantime — the attempt itself was accepted before the deadline lapsed.
        if row.status in (STATUS_LOCKED, STATUS_COMPLETED):
            continue
        row.status = STATUS_COMPLETED
        row.submitted_at = now
        row.quiz_attempt_id = attempt.id
        row.correct_answers = attempt.correct_answers
        row.total_questions = attempt.total_questions
        row.percentage = round(float(attempt.score_percentage or 0.0), 2)
        done.append(row)
    if done:
        db.flush()
        if commit:
            db.commit()
    return done


def checkpoint_quiz_lesson_ids(db: Session) -> set:
    """Every lesson that carries a checkpoint quiz (all definitions, active or not)."""
    return {r[0] for r in db.query(CheckpointDefinition.quiz_lesson_id).filter(
        CheckpointDefinition.quiz_lesson_id.isnot(None)).all()}


def open_checkpoint_lesson_ids_for_student(db: Session, student_id: int) -> set:
    """Quiz lessons this student may actually open: a non-locked row, in a group that still has
    checkpoints enabled, on a definition that is still active."""
    return {r[0] for r in db.query(CheckpointDefinition.quiz_lesson_id).join(
        StudentCheckpoint, StudentCheckpoint.checkpoint_id == CheckpointDefinition.id
    ).join(Group, Group.id == StudentCheckpoint.group_id).filter(
        StudentCheckpoint.student_id == student_id,
        StudentCheckpoint.status != STATUS_LOCKED,
        CheckpointDefinition.quiz_lesson_id.isnot(None),
        CheckpointDefinition.is_active == True,  # noqa: E712
        Group.checkpoints_enabled == True,  # noqa: E712
    ).all()}


def student_may_view_checkpoint_lesson(db: Session, user, lesson_id: int) -> bool:
    """False only for a student looking at a checkpoint quiz lesson that is not open for them.

    Needed because `check_course_access` grants the hidden quiz course as a whole and every
    checkpoint lesson is `is_initially_unlocked=True` — without this, one open checkpoint would
    expose the questions (and `correct_answer`) of all the others.
    """
    if getattr(user, "role", None) != "student":
        return True
    if lesson_id not in checkpoint_quiz_lesson_ids(db):
        return True
    return lesson_id in open_checkpoint_lesson_ids_for_student(db, user.id)


CHECKPOINT_LESSON_DENIED = "This checkpoint is not open for you"


def assert_student_may_view_checkpoint_lesson(db: Session, user, lesson_id: int) -> None:
    if not student_may_view_checkpoint_lesson(db, user, lesson_id):
        raise HTTPException(status_code=403, detail=CHECKPOINT_LESSON_DENIED)


def student_has_checkpoint_access_to_course(db: Session, student_id: int, course_id: int) -> bool:
    """Hook for check_course_access: a student may enter the hidden quiz course only through a
    non-locked checkpoint row whose quiz lesson lives in that course."""
    return db.query(StudentCheckpoint.id).join(
        CheckpointDefinition, CheckpointDefinition.id == StudentCheckpoint.checkpoint_id
    ).join(Lesson, Lesson.id == CheckpointDefinition.quiz_lesson_id
    ).join(Module, Module.id == Lesson.module_id
    ).filter(
        StudentCheckpoint.student_id == student_id,
        StudentCheckpoint.status != STATUS_LOCKED,
        Module.course_id == course_id,
    ).first() is not None


# ---------------------------------------------------------------- serializers

def _iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else naive(dt).isoformat() + "Z"


def quiz_ref(db: Session, definition: CheckpointDefinition) -> Optional[Dict[str, int]]:
    if definition.quiz_lesson_id is None:
        return None
    course_id = db.query(Module.course_id).join(Lesson, Lesson.module_id == Module.id).filter(
        Lesson.id == definition.quiz_lesson_id).scalar()
    if course_id is None:
        return None
    return {"course_id": int(course_id), "lesson_id": int(definition.quiz_lesson_id)}


def serialize_row(row: Optional[StudentCheckpoint]) -> Dict[str, Any]:
    if row is None:
        return {"id": None, "status": STATUS_LOCKED, "opened_at": None, "deadline": None, "submitted_at": None,
                "correct_answers": None, "total_questions": None, "percentage": None,
                "opened_by": None, "reopen_count": 0, "quiz_attempt_id": None}
    return {
        "id": row.id, "status": row.status, "opened_at": _iso(row.opened_at), "deadline": _iso(row.deadline),
        "submitted_at": _iso(row.submitted_at), "correct_answers": row.correct_answers,
        "total_questions": row.total_questions, "percentage": row.percentage,
        "opened_by": row.opened_by, "reopen_count": row.reopen_count or 0, "quiz_attempt_id": row.quiz_attempt_id,
    }


def serialize_for_student(db: Session, student_id: int, group: Group, definition: CheckpointDefinition,
                          row: Optional[StudentCheckpoint]) -> Dict[str, Any]:
    units = unit_progress(db, student_id, definition)
    base = serialize_row(row)
    status = base["status"]
    base.update({
        "checkpoint_id": definition.id,
        "number": definition.number,
        "title": definition.title,
        "group_id": group.id,
        "group_name": group.name,
        "covers": units,
        "total_questions": base["total_questions"] or definition.total_questions,
        "locked_reason": locked_reason(units) if status == STATUS_LOCKED else None,
        "quiz": quiz_ref(db, definition) if status != STATUS_LOCKED else None,
    })
    return base
