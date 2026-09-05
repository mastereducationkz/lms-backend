"""SAT Checkpoints domain logic (see docs/superpowers/plans/2026-09-03-sat-checkpoints.md).

Trigger rule (ТЗ §4-§6, §10): a checkpoint opens for a student when ALL required units of its
block are completed by that student — nothing else (no calendar week, no lesson counts, no
attendance). Deadline = opened_at + 24h, and it is soft: a late submission is accepted and
flagged (assert_can_submit / serialize_row). Rows are per (student, group, checkpoint).
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.checkpoints.completion import completed_lesson_ids
from src.checkpoints.models import (
    OPEN_STATUSES, STATUS_AVAILABLE, STATUS_COMPLETED, STATUS_LOCKED, STATUS_OVERDUE,
    STATUS_REOPENED, CheckpointDefinition, StudentCheckpoint,
)
from src.courses.models import CourseGroupAccess, Group, GroupStudent, Lesson, Module, Step
from src.progress.models import QuizAttempt
from src.services.cache_service import invalidate
from src.utils.permissions import get_group_course_ids

_log = logging.getLogger(__name__)

DEADLINE_HOURS = 24
# Rows a quiz attempt may land on. `overdue` is included on purpose: the deadline is soft, a late
# submission is recorded and flagged rather than refused.
SUBMITTABLE_STATUSES = (STATUS_AVAILABLE, STATUS_REOPENED, STATUS_OVERDUE)

# The four per-user lesson caches whose payloads depend on which checkpoints are open for a
# student (see _checkpoint_filter_lesson_payload / assert_student_may_view_checkpoint_lesson).
# TTLs run to 300s, so without this a completed or lapsed checkpoint would keep serving its
# questions — and a freshly opened one would stay invisible — for minutes.
LESSON_CACHE_PATTERNS = ("courses:lesson:*", "courses:lesson-steps:*",
                         "courses:module-lessons:*", "courses:lessons-list:*")


def _invalidate_lesson_caches() -> None:
    """Drop the lesson caches after a checkpoint status change. Never raises: Redis being
    unavailable must not fail the request that changed the checkpoint."""
    try:
        invalidate(*LESSON_CACHE_PATTERNS)
    except Exception as exc:                                     # pragma: no cover - defensive
        _log.warning("checkpoint lesson-cache invalidation failed: %s", exc)


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


def unit_options(db: Session, course_id: int) -> List[Dict[str, Any]]:
    """Every unit of the course a checkpoint may require, with the kind its module implies
    ('Verbal' -> verbal, 'Math' -> math). Checkpoint lessons and the 'Unit 0' onboarding are
    left out; modules of any other name are not checkpoint material."""
    rows = db.query(Lesson.id, Lesson.title, Module.title).join(
        Module, Module.id == Lesson.module_id
    ).filter(
        Module.course_id == course_id, Lesson.kind != "checkpoint",
    ).order_by(Module.order_index, Lesson.order_index, Lesson.id).all()
    out = []
    for lesson_id, title, module_title in rows:
        low = (module_title or "").lower()
        kind = "verbal" if "verbal" in low else "math" if "math" in low else None
        if kind is None or (title or "").strip().lower().startswith("unit 0"):
            continue
        out.append({"lesson_id": lesson_id, "title": title, "module": module_title, "kind": kind})
    return out


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


# ---------------------------------------------------------------- batched context

class StudentCheckpointContext:
    """Every read GET /checkpoints/me needs, done once instead of once per checkpoint.

    Serving 9 checkpoints used to cost ~55 statements (per-item unit progress, lesson titles and
    quiz-course lookups); with this the whole route is a fixed handful regardless of how many
    checkpoints or groups the student has.
    """

    __slots__ = ("definitions_by_group", "rows", "completed", "titles", "quiz_course_by_lesson",
                 "completed_elsewhere")

    def __init__(self, definitions_by_group, rows, completed, titles, quiz_course_by_lesson,
                 completed_elsewhere=None):
        self.definitions_by_group = definitions_by_group
        self.rows = rows
        self.completed = completed
        self.titles = titles
        self.quiz_course_by_lesson = quiz_course_by_lesson
        # checkpoint_id -> a completed row of this student in ANY group (transfer carry-over)
        self.completed_elsewhere = completed_elsewhere or {}


def student_checkpoint_context(db: Session, student_id: int,
                               groups: List[Group]) -> StudentCheckpointContext:
    group_ids = [g.id for g in groups]
    course_ids_by_group: Dict[int, List[int]] = {gid: [] for gid in group_ids}
    if group_ids:
        for gid, cid in db.query(CourseGroupAccess.group_id, CourseGroupAccess.course_id).filter(
            CourseGroupAccess.group_id.in_(group_ids),
            CourseGroupAccess.is_active == True,  # noqa: E712
        ).all():
            course_ids_by_group[gid].append(cid)

    course_ids = sorted({cid for cids in course_ids_by_group.values() for cid in cids})
    definitions = db.query(CheckpointDefinition).filter(
        CheckpointDefinition.course_id.in_(course_ids),
        CheckpointDefinition.is_active == True,  # noqa: E712
    ).order_by(CheckpointDefinition.number).all() if course_ids else []
    by_course: Dict[int, List[CheckpointDefinition]] = {}
    for d in definitions:
        by_course.setdefault(d.course_id, []).append(d)
    definitions_by_group: Dict[int, List[CheckpointDefinition]] = {}
    for gid in group_ids:
        seen = {d.id: d for cid in course_ids_by_group[gid] for d in by_course.get(cid, [])}
        definitions_by_group[gid] = sorted(seen.values(), key=lambda d: d.number)

    rows: Dict[tuple, StudentCheckpoint] = {}
    completed_elsewhere: Dict[int, StudentCheckpoint] = {}
    if group_ids:
        # One query over every row of the student, not just the enabled groups': the rows of a
        # previous group are what a transfer carries over (see _carry_over).
        for r in db.query(StudentCheckpoint).filter(
            StudentCheckpoint.student_id == student_id,
        ).order_by(StudentCheckpoint.submitted_at.desc().nullslast(), StudentCheckpoint.id.desc()).all():
            if r.group_id in course_ids_by_group:
                rows[(r.group_id, r.checkpoint_id)] = r
            if r.status == STATUS_COMPLETED:
                completed_elsewhere.setdefault(r.checkpoint_id, r)

    required_ids = list(dict.fromkeys(
        u.lesson_id for ds in definitions_by_group.values() for d in ds for u in d.required_units))
    completed = completed_lesson_ids(db, student_id, required_ids)
    titles = dict(db.query(Lesson.id, Lesson.title).filter(
        Lesson.id.in_(required_ids)).all()) if required_ids else {}

    quiz_lesson_ids = list(dict.fromkeys(
        d.quiz_lesson_id for ds in definitions_by_group.values() for d in ds
        if d.quiz_lesson_id is not None))
    quiz_course_by_lesson = dict(db.query(Lesson.id, Module.course_id).join(
        Module, Module.id == Lesson.module_id).filter(
        Lesson.id.in_(quiz_lesson_ids)).all()) if quiz_lesson_ids else {}

    return StudentCheckpointContext(definitions_by_group, rows, completed, titles, quiz_course_by_lesson,
                                    completed_elsewhere)


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


def _completed_elsewhere(db: Session, student_id: int, definition_ids: List[int]) -> Dict[int, StudentCheckpoint]:
    """This student's completed rows for these definitions in ANY group. A student who moves to
    another group keeps the checkpoints they already passed instead of retaking them."""
    if not definition_ids:
        return {}
    out: Dict[int, StudentCheckpoint] = {}
    for r in db.query(StudentCheckpoint).filter(
        StudentCheckpoint.student_id == student_id,
        StudentCheckpoint.checkpoint_id.in_(definition_ids),
        StudentCheckpoint.status == STATUS_COMPLETED,
    ).order_by(StudentCheckpoint.submitted_at.desc().nullslast(), StudentCheckpoint.id.desc()).all():
        out.setdefault(r.checkpoint_id, r)
    return out


def _carry_over(row: Optional[StudentCheckpoint], previous: StudentCheckpoint, *, student_id: int, group_id: int,
                definition: CheckpointDefinition, now: datetime) -> StudentCheckpoint:
    """Copy a checkpoint passed in another group onto this group's row (opened_by 'transfer')."""
    if row is None:
        row = StudentCheckpoint(student_id=student_id, group_id=group_id,
                                checkpoint_id=definition.id, checkpoint_number=definition.number)
    row.checkpoint_number = definition.number
    row.required_unit_ids = [u.lesson_id for u in definition.required_units]
    row.status = STATUS_COMPLETED
    row.opened_at = previous.opened_at or now
    row.deadline = previous.deadline
    row.submitted_at = previous.submitted_at or now
    row.quiz_attempt_id = previous.quiz_attempt_id
    row.correct_answers = previous.correct_answers
    row.total_questions = previous.total_questions
    row.percentage = previous.percentage
    row.opened_by = "transfer"
    row.updated_by = None
    return row


def _open_eligible_checkpoints(db: Session, student_id: int, now: datetime, keys: List[tuple], *,
                               groups: Optional[List[Group]] = None,
                               ctx: Optional[StudentCheckpointContext] = None) -> List[StudentCheckpoint]:
    if groups is None:
        groups = enabled_groups_for_student(db, student_id)
    opened: List[StudentCheckpoint] = []
    for group in groups:
        definitions = (ctx.definitions_by_group.get(group.id, []) if ctx is not None
                       else definitions_for_group(db, group))
        eligible: List[Tuple[CheckpointDefinition, Optional[StudentCheckpoint]]] = []
        for definition in definitions:
            if definition.number < (group.checkpoints_start_number or 1):
                continue
            required = [u.lesson_id for u in definition.required_units]
            if not required:
                continue
            row = (ctx.rows.get((group.id, definition.id)) if ctx is not None
                   else get_row(db, student_id, group.id, definition.id))
            if row is not None and row.status != STATUS_LOCKED:
                continue
            done = ctx.completed if ctx is not None else completed_lesson_ids(db, student_id, required)
            if set(required) <= done:
                eligible.append((definition, row))
        if not eligible:
            continue
        eligible.sort(key=lambda pair: pair[0].number)
        carried = (ctx.completed_elsewhere if ctx is not None
                   else _completed_elsewhere(db, student_id, [d.id for d, _ in eligible]))
        stagger = 0
        for definition, row in eligible:
            previous = carried.get(definition.id)
            if previous is not None and previous.group_id != group.id:
                # Passed in a previous group: carry the result over, nothing to retake.
                row = _carry_over(row, previous, student_id=student_id, group_id=group.id,
                                  definition=definition, now=now)
            else:
                # Several checkpoints opening in the same moment — a student who joins mid-course,
                # or a group switched on late — get their deadlines a day apart in checkpoint order
                # instead of all falling due at once.
                row = _open_row(row, student_id=student_id, group_id=group.id, definition=definition,
                                now=now, opened_by="auto",
                                deadline=now + timedelta(hours=DEADLINE_HOURS * (stagger + 1)), actor_id=None)
                stagger += 1
                opened.append(row)
            db.add(row)
            keys.append((group.id, definition.id))
            if ctx is not None:
                ctx.rows[(group.id, definition.id)] = row
    return opened


def _rows_for_keys(db: Session, student_id: int, keys: List[tuple]) -> List[StudentCheckpoint]:
    if not keys:
        return []
    return db.query(StudentCheckpoint).filter(
        StudentCheckpoint.student_id == student_id,
        tuple_(StudentCheckpoint.group_id, StudentCheckpoint.checkpoint_id).in_(keys),
        StudentCheckpoint.status != STATUS_LOCKED,
    ).order_by(StudentCheckpoint.id).all()


def sync_student_checkpoints(db: Session, student_id: int, *, now: Optional[datetime] = None,
                             commit: bool = True, groups: Optional[List[Group]] = None,
                             ctx: Optional[StudentCheckpointContext] = None) -> List[StudentCheckpoint]:
    """Open every checkpoint whose required units the student has completed, in every enabled
    group. Idempotent: rows that are already open/completed/overdue/reopened are left alone.

    This is read-then-insert, so two workers (4 uvicorn processes in prod) can both decide to open
    the same (student, group, checkpoint) and the loser's INSERT trips uq_student_checkpoint. That
    is a benign race — the row exists either way — so we swallow the IntegrityError and report the
    rows that are now actually there instead of 500-ing the request that triggered the sync.
    """
    now = now or utcnow()
    keys: List[tuple] = []
    try:
        opened = _open_eligible_checkpoints(db, student_id, now, keys, groups=groups, ctx=ctx)
        if keys:                      # opened rows, or results carried over from another group
            db.flush()
            if commit:
                db.commit()
            _invalidate_lesson_caches()
        return opened
    except IntegrityError:
        _log.info("checkpoint open raced for student %s (keys=%s); reusing the existing rows",
                  student_id, keys)
        db.rollback()
        existing = _rows_for_keys(db, student_id, keys)
        if ctx is not None:
            for key in keys:
                ctx.rows.pop(key, None)
            for r in existing:
                ctx.rows[(r.group_id, r.checkpoint_id)] = r
        return existing


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
    """Flip lapsed rows to overdue. An overdue checkpoint can still be submitted (late) and
    still holds the next block back; the flip changes what the student is told, and the cached
    lesson payloads carry that, so the lesson caches are dropped when anything flips."""
    now = now or utcnow()
    flipped = []
    for row in rows:
        if row.status in OPEN_STATUSES and row.deadline is not None and naive(row.deadline) < now:
            row.status = STATUS_OVERDUE
            flipped.append(row)
    if flipped:
        _invalidate_lesson_caches()
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
    if changed:
        _invalidate_lesson_caches()
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
    if changed:
        _invalidate_lesson_caches()
    return changed


def set_deadline(db: Session, row: StudentCheckpoint, deadline: datetime, actor_id: int,
                 now: Optional[datetime] = None, commit: bool = True) -> StudentCheckpoint:
    """Change a row's deadline (and un-overdue it if the new deadline is in the future). Moving a
    row from overdue back to available re-blocks whatever checkpoint-bound units it was holding
    back, so that transition invalidates the lesson caches; a plain deadline change with no status
    transition does not."""
    now = now or utcnow()
    row.deadline = naive(deadline)
    row.updated_by = actor_id
    was_overdue = row.status == STATUS_OVERDUE
    if was_overdue and row.deadline > now:
        row.status = STATUS_AVAILABLE
    db.flush()
    if commit:
        db.commit()
    if was_overdue and row.status == STATUS_AVAILABLE:
        _invalidate_lesson_caches()
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


def _live_rows_for_student_definition(db: Session, student_id: int,
                                      definition: CheckpointDefinition) -> List[StudentCheckpoint]:
    """Rows that still confer anything: the definition must still be active AND the row's group
    must still be live with checkpoints enabled. Disabling any of the three revokes access, it
    does not merely hide."""
    if not definition.is_active:
        return []
    return db.query(StudentCheckpoint).join(
        Group, Group.id == StudentCheckpoint.group_id
    ).filter(
        StudentCheckpoint.student_id == student_id,
        StudentCheckpoint.checkpoint_id == definition.id,
        Group.checkpoints_enabled == True,  # noqa: E712
        Group.is_active == True,  # noqa: E712
    ).order_by(StudentCheckpoint.id).all()


def assert_can_submit(db: Session, student_id: int, definition: CheckpointDefinition,
                      now: Optional[datetime] = None) -> List[StudentCheckpoint]:
    """Server-side gate for a checkpoint quiz attempt, drafts and final submissions alike.

    A locked checkpoint refuses (403). A completed one refuses another attempt (409) until an
    admin reopens it. The deadline is soft (user decision 2026-09-05): a row past its deadline —
    still `available`/`reopened`, or already flipped to `overdue` — accepts the submission, which
    record_submission stores with its timestamp so staff see exactly how late it was.
    """
    now = now or utcnow()
    rows = _live_rows_for_student_definition(db, student_id, definition)
    if not rows or all(r.status == STATUS_LOCKED for r in rows):
        raise HTTPException(status_code=403, detail="Checkpoint is locked: complete the required units first")
    submittable = [r for r in rows if r.status in SUBMITTABLE_STATUSES]
    if not submittable:
        raise HTTPException(status_code=409, detail="Checkpoint already completed")
    refresh_overdue(submittable, now)   # a lapsed row is flagged overdue as it is being answered
    return submittable


def record_submission(db: Session, student_id: int, attempt: QuizAttempt,
                      now: Optional[datetime] = None, commit: bool = True) -> List[StudentCheckpoint]:
    """Copy a finalized quiz attempt into every open row of that checkpoint for the student.

    Completing a checkpoint now unblocks the units of any later checkpoint that were held back by
    it (see blocked_unit_lesson_ids_for_student), so this invalidates the lesson caches."""
    now = now or utcnow()
    definition = checkpoint_definition_for_step(db, attempt.step_id)
    if definition is None or attempt.is_draft:
        return []
    done = []
    for row in _rows_for_student_definition(db, student_id, definition):
        # Record onto every submittable row (available / reopened / overdue). The deadline is
        # soft: an overdue row completes too, and serialize_row reports it as late by comparing
        # submitted_at with the deadline.
        if row.status not in SUBMITTABLE_STATUSES:
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
        _invalidate_lesson_caches()
    return done


def checkpoint_quiz_lesson_ids(db: Session) -> set:
    """Every lesson that carries a checkpoint quiz (all definitions, active or not)."""
    return {r[0] for r in db.query(CheckpointDefinition.quiz_lesson_id).filter(
        CheckpointDefinition.quiz_lesson_id.isnot(None)).all()}


def checkpoint_visibility(db: Session, user_id: int, *,
                           all_lesson_ids: Optional[Set[int]] = None) -> Tuple[Set[int], Set[int]]:
    """(every checkpoint quiz lesson, the ones this student may see at all).

    A student in no checkpoints-enabled, active group sees none of them — the feature is invisible
    outside the pilot. Inside an enabled group every checkpoint of that group's courses is listed;
    whether each one is *enterable* is a separate question answered by
    ``open_checkpoint_lesson_ids_for_student``.

    ``all_lesson_ids`` lets a caller that already ran ``checkpoint_quiz_lesson_ids(db)`` this
    request (e.g. ``get_course_modules``) pass it in and skip the repeat query.
    """
    all_ids = checkpoint_quiz_lesson_ids(db) if all_lesson_ids is None else all_lesson_ids
    if not all_ids:
        return set(), set()
    course_ids = {
        cid
        for group in enabled_groups_for_student(db, user_id)
        for cid in get_group_course_ids(db, group.id)
    }
    if not course_ids:
        return all_ids, set()
    visible = {
        row[0]
        for row in db.query(CheckpointDefinition.quiz_lesson_id).filter(
            CheckpointDefinition.course_id.in_(course_ids),
            CheckpointDefinition.quiz_lesson_id.isnot(None),
        ).all()
    }
    return all_ids, visible


def open_checkpoint_lesson_ids_for_student(db: Session, student_id: int) -> set:
    """Quiz lessons this student may actually open: a non-locked row, in a live group that still
    has checkpoints enabled, on a definition that is still active."""
    return {r[0] for r in db.query(CheckpointDefinition.quiz_lesson_id).join(
        StudentCheckpoint, StudentCheckpoint.checkpoint_id == CheckpointDefinition.id
    ).join(Group, Group.id == StudentCheckpoint.group_id).filter(
        StudentCheckpoint.student_id == student_id,
        StudentCheckpoint.status != STATUS_LOCKED,
        CheckpointDefinition.quiz_lesson_id.isnot(None),
        CheckpointDefinition.is_active == True,  # noqa: E712
        Group.checkpoints_enabled == True,  # noqa: E712
        Group.is_active == True,  # noqa: E712
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
    ).join(Group, Group.id == StudentCheckpoint.group_id
    ).filter(
        StudentCheckpoint.student_id == student_id,
        StudentCheckpoint.status != STATUS_LOCKED,
        CheckpointDefinition.is_active == True,  # noqa: E712
        Group.checkpoints_enabled == True,  # noqa: E712
        Group.is_active == True,  # noqa: E712
        Module.course_id == course_id,
    ).first() is not None


# ---------------------------------------------------------------- blocked-unit rule
#
# The units of block N are blocked until the checkpoint of block N-1 is *cleared* — not merely
# "not pending". The earlier statuses-only rule (block whatever's required units are incomplete
# while some row of theirs sits available/reopened) only ever caught a checkpoint that had
# actually opened, which requires ALL of its required units to be done first. A student who simply
# never does one required unit (e.g. skips the Math unit of a block) never opens that checkpoint,
# so nothing was ever "pending" and every later block's units ran open unchecked. The ordinal rule
# below closes that hole: block N is blocked by definition whenever ANY earlier block in the same
# course is not cleared, whether or not that earlier checkpoint ever opened at all.

# A row clears its checkpoint only once it is `completed` (on time or late). Since 2026-09-05
# the deadline is soft — an overdue checkpoint can still be submitted, flagged late — so a lapsed
# deadline no longer opens the gate on its own: the student is never stranded (they can submit
# any time), and the next block waits for the submission. Every other status — no row at all,
# `locked`, `available`, `reopened`, `overdue` — holds the next block back. A checkpoint skipped
# by the group's start number is treated as cleared (see is_skipped).
CLEARING_STATUSES = (STATUS_COMPLETED,)


def is_skipped(group: Group, definition: CheckpointDefinition, row: Optional[StudentCheckpoint]) -> bool:
    """A checkpoint numbered below the group's checkpoints_start_number that never got a row was
    passed over on purpose (the group joined mid-course): it never auto-opens and never gates
    later blocks. An admin may still open it by hand, after which it behaves like any other."""
    return row is None and definition.number < (group.checkpoints_start_number or 1)


def skipped_reason(group: Group) -> str:
    return f"Not required for this group — it starts from Checkpoint {group.checkpoints_start_number or 1}"


def _checkpoint_chain_for_student(
    db: Session, user_id: int
) -> Dict[int, List[Tuple[CheckpointDefinition, bool]]]:
    """Per course, that course's ACTIVE checkpoint definitions in `number` order, each paired
    with whether this student has cleared it. Only touches courses reached through a group that
    is `checkpoints_enabled` AND `is_active` for this student (the pilot scoping) — a student
    outside that, and any staff member (no such group membership), gets `{}` via the cheapest
    path: `enabled_groups_for_student` alone. A gap in one course's chain never affects another —
    each course's chain is built and walked independently.
    """
    groups = enabled_groups_for_student(db, user_id)
    if not groups:
        return {}
    # Per course, the lowest checkpoints_start_number among the enabled groups that reach it: a
    # definition numbered below that was skipped on purpose for a mid-course group (is_skipped).
    start_by_course: Dict[int, int] = {}
    for group in groups:
        start = group.checkpoints_start_number or 1
        for cid in get_group_course_ids(db, group.id):
            start_by_course[cid] = min(start_by_course.get(cid, start), start)
    course_ids = set(start_by_course)
    if not course_ids:
        return {}
    definitions = (
        db.query(CheckpointDefinition)
        .filter(
            CheckpointDefinition.course_id.in_(course_ids),
            CheckpointDefinition.is_active == True,  # noqa: E712
        )
        .order_by(CheckpointDefinition.course_id, CheckpointDefinition.number)
        .all()
    )
    if not definitions:
        return {}

    def_ids = [d.id for d in definitions]
    # The student's own rows for these definitions, but only the ones that still come from a
    # checkpoints-enabled, active group (existing Group join) — matches the scoping everywhere
    # else in this module.
    cleared_by_def_id: Dict[int, bool] = {}
    for checkpoint_id, status in (
        db.query(StudentCheckpoint.checkpoint_id, StudentCheckpoint.status)
        .join(Group, Group.id == StudentCheckpoint.group_id)
        .filter(
            StudentCheckpoint.student_id == user_id,
            StudentCheckpoint.checkpoint_id.in_(def_ids),
            Group.checkpoints_enabled == True,  # noqa: E712
            Group.is_active == True,  # noqa: E712
        )
        .all()
    ):
        cleared_by_def_id[checkpoint_id] = cleared_by_def_id.get(checkpoint_id, False) or (
            status in CLEARING_STATUSES
        )

    by_course: Dict[int, List[Tuple[CheckpointDefinition, bool]]] = {}
    for definition in definitions:
        if definition.id in cleared_by_def_id:
            cleared = cleared_by_def_id[definition.id]
        else:
            # No row at all. Below the group's start number the checkpoint was skipped on purpose
            # (see is_skipped), so it must not hold later blocks back; at or above it, the
            # checkpoint simply has not been cleared yet.
            cleared = definition.number < start_by_course.get(definition.course_id, 1)
        by_course.setdefault(definition.course_id, []).append((definition, cleared))
    return by_course


def blocking_checkpoint_for_student(db: Session, user_id: int) -> Optional[CheckpointDefinition]:
    """The FIRST not-cleared checkpoint definition — the one the student actually has to finish to
    release every block behind it. `None` once every checkpoint in every enabled course is
    cleared (or the student has none)."""
    by_course = _checkpoint_chain_for_student(db, user_id)
    candidates: List[CheckpointDefinition] = []
    for chain in by_course.values():
        for definition, cleared in chain:
            if not cleared:
                candidates.append(definition)
                break
    if not candidates:
        return None
    candidates.sort(key=lambda d: (d.course_id, d.number))
    return candidates[0]


def blocked_unit_lesson_ids_for_student(db: Session, user_id: int) -> Set[int]:
    """Units the student may not start yet under the ordinal gate: block N's required units are
    blocked whenever some earlier block in that course is not cleared (see CLEARING_STATUSES).
    Block 1 of a course is never blocked — nothing precedes it. Units bound to no checkpoint are
    never blocked, and an already-completed unit stays open so past material can be reviewed.
    Empty for every student outside a checkpoints-enabled group — the feature must not touch
    anyone else.
    """
    by_course = _checkpoint_chain_for_student(db, user_id)
    if not by_course:
        return set()
    blocked_ids: Set[int] = set()
    for chain in by_course.values():
        every_earlier_cleared = True
        for definition, cleared in chain:
            if not every_earlier_cleared:
                blocked_ids.update(u.lesson_id for u in definition.required_units)
            every_earlier_cleared = every_earlier_cleared and cleared
    if not blocked_ids:
        return set()
    return blocked_ids - completed_lesson_ids(db, user_id, blocked_ids)


def assert_student_not_blocked_by_checkpoint(db: Session, user, lesson_id: int) -> None:
    """403 for a unit the student must not start until their pending checkpoint is done."""
    if getattr(user, "role", None) != "student":
        return
    if lesson_id not in blocked_unit_lesson_ids_for_student(db, user.id):
        return
    definition = blocking_checkpoint_for_student(db, user.id)
    name = definition.title if definition is not None else "your checkpoint"
    raise HTTPException(status_code=403, detail=f"Finish {name} before starting this unit")


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


def late_minutes(row: Optional[StudentCheckpoint]) -> Optional[int]:
    """Minutes between the deadline and the submission when the submission came after it;
    None when the row was not submitted, has no deadline, or was submitted on time."""
    if row is None or row.submitted_at is None or row.deadline is None:
        return None
    delta = naive(row.submitted_at) - naive(row.deadline)
    minutes = int(delta.total_seconds() // 60)
    return minutes if minutes > 0 else None


def serialize_row(row: Optional[StudentCheckpoint]) -> Dict[str, Any]:
    if row is None:
        return {"id": None, "status": STATUS_LOCKED, "opened_at": None, "deadline": None, "submitted_at": None,
                "correct_answers": None, "total_questions": None, "percentage": None,
                "opened_by": None, "reopen_count": 0, "quiz_attempt_id": None,
                "late": False, "late_minutes": None}
    late = late_minutes(row)
    return {
        "id": row.id, "status": row.status, "opened_at": _iso(row.opened_at), "deadline": _iso(row.deadline),
        "submitted_at": _iso(row.submitted_at), "correct_answers": row.correct_answers,
        "total_questions": row.total_questions, "percentage": row.percentage,
        "opened_by": row.opened_by, "reopen_count": row.reopen_count or 0, "quiz_attempt_id": row.quiz_attempt_id,
        "late": late is not None, "late_minutes": late,
    }


def _serialize_item(group: Group, definition: CheckpointDefinition,
                    row: Optional[StudentCheckpoint], ctx: StudentCheckpointContext) -> Dict[str, Any]:
    """Same payload as serialize_for_student, but every lookup comes from the prefetched context."""
    units = [{"lesson_id": u.lesson_id, "title": ctx.titles.get(u.lesson_id, ""), "kind": u.kind,
              "completed": u.lesson_id in ctx.completed} for u in definition.required_units]
    base = serialize_row(row)
    status = base["status"]
    skipped = is_skipped(group, definition, row)
    quiz = None
    if status != STATUS_LOCKED and definition.quiz_lesson_id is not None:
        course_id = ctx.quiz_course_by_lesson.get(definition.quiz_lesson_id)
        if course_id is not None:
            quiz = {"course_id": int(course_id), "lesson_id": int(definition.quiz_lesson_id)}
    base.update({
        "checkpoint_id": definition.id,
        "number": definition.number,
        "title": definition.title,
        "group_id": group.id,
        "group_name": group.name,
        "covers": units,
        "total_questions": base["total_questions"] or definition.total_questions,
        "skipped": skipped,
        "locked_reason": (skipped_reason(group) if skipped else locked_reason(units)) if status == STATUS_LOCKED else None,
        "quiz": quiz,
    })
    return base


def serialize_items_for_student(db: Session, student_id: int, groups: List[Group], *,
                                ctx: Optional[StudentCheckpointContext] = None) -> List[Dict[str, Any]]:
    """Every checkpoint item for a student, in one batch of queries."""
    if ctx is None:
        ctx = student_checkpoint_context(db, student_id, groups)
    return [_serialize_item(group, definition, ctx.rows.get((group.id, definition.id)), ctx)
            for group in groups
            for definition in ctx.definitions_by_group.get(group.id, [])]


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
        "skipped": is_skipped(group, definition, row),
        "locked_reason": ((skipped_reason(group) if is_skipped(group, definition, row) else locked_reason(units))
                          if status == STATUS_LOCKED else None),
        "quiz": quiz_ref(db, definition) if status != STATUS_LOCKED else None,
    })
    return base
