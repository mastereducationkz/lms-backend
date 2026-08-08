"""Exam-results domain services.

Contact resolution and grid assembly both run over whole groups, so every lookup here
is batched. A per-student query would turn the Bluebook grid into an N+1 across every
row on every render.
"""
from collections import defaultdict
from datetime import date
from statistics import mean, median
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from src.assignments.models import Assignment, AssignmentZeroSubmission
from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent
from src.exams.models import BluebookResult, ExamResult
from src.exams.schemas import (
    BluebookCell,
    BluebookColumn,
    BluebookColumnStats,
    BluebookGridOut,
    BluebookStudentRow,
    ExamResultOut,
    StudentContactOut,
)
from src.parents.models import ParentStudent


# --------------------------------------------------------------------------------------
# Contact resolution
# --------------------------------------------------------------------------------------

def resolve_student_contacts(db: Session, student_ids: Sequence[int]) -> Dict[int, StudentContactOut]:
    """Batch-resolve the authorized contact block for a set of students.

    Source of truth per field, established by inspection of the schema:

    * full name      - ``users.official_full_name`` when set, else ``users.name``
    * student phone  - Assignment Zero only (``users`` has no phone column at all)
    * telegram tag   - Assignment Zero only
    * parent phone   - Assignment Zero only
    * parent name    - the linked ``role='parent'`` user's ``name``; there is no
                       parent-name field on Assignment Zero and no join between the
                       bare ``parent_phone_number`` string and a parent account

    Anything unknown stays ``None`` and is rendered as absent. Nothing is synthesized:
    a missing parent name means no parent account is linked, and inventing one from
    the student's surname would be a fabrication in an export used for outreach.
    """
    ids = [i for i in set(student_ids) if i]
    if not ids:
        return {}

    users = {u.id: u for u in db.query(UserInDB).filter(UserInDB.id.in_(ids)).all()}

    az_rows = (
        db.query(
            AssignmentZeroSubmission.user_id,
            AssignmentZeroSubmission.phone_number,
            AssignmentZeroSubmission.telegram_id,
            AssignmentZeroSubmission.parent_phone_number,
        )
        .filter(AssignmentZeroSubmission.user_id.in_(ids))
        .all()
    )
    az_by_student = {r[0]: r for r in az_rows}

    # Primary parent first, so a student with several linked guardians resolves
    # deterministically instead of by insertion order.
    parent_rows = (
        db.query(ParentStudent.student_id, ParentStudent.is_primary, UserInDB.name)
        .join(UserInDB, UserInDB.id == ParentStudent.parent_id)
        .filter(ParentStudent.student_id.in_(ids))
        .all()
    )
    parent_name_by_student: Dict[int, str] = {}
    for student_id, is_primary, parent_name in parent_rows:
        if not parent_name:
            continue
        if is_primary or student_id not in parent_name_by_student:
            parent_name_by_student[student_id] = parent_name

    out: Dict[int, StudentContactOut] = {}
    for sid in ids:
        user = users.get(sid)
        if user is None:
            continue
        az = az_by_student.get(sid)
        out[sid] = StudentContactOut(
            student_id=sid,
            full_name=(user.official_full_name or user.name or "").strip(),
            student_phone=_clean(az[1]) if az else None,
            telegram_tag=_clean(az[2]) if az else None,
            parent_phone=_clean(az[3]) if az else None,
            parent_full_name=parent_name_by_student.get(sid),
        )
    return out


def _clean(value: Optional[str]) -> Optional[str]:
    """Empty and whitespace-only strings are absent data, not values.

    Assignment Zero declares these columns NOT NULL, so "unknown" is stored as an
    empty string. Reporting that as a phone number would be misleading.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


# --------------------------------------------------------------------------------------
# Planned dates (read from Assignment Zero - the established source of truth)
# --------------------------------------------------------------------------------------

def resolve_planned_dates(db: Session, student_ids: Sequence[int], exam_type: str) -> Dict[int, Optional[date]]:
    """Planned/expected exam dates, read from Assignment Zero.

    This domain deliberately does not store planned dates - Assignment Zero already
    owns them, and a second copy would be a parallel calendar that drifts.
    """
    ids = [i for i in set(student_ids) if i]
    if not ids:
        return {}

    if exam_type == "sat":
        column = AssignmentZeroSubmission.sat_planned_test_date
    elif exam_type == "ielts":
        column = AssignmentZeroSubmission.ielts_planned_test_date
    else:
        return {i: None for i in ids}

    rows = (
        db.query(AssignmentZeroSubmission.user_id, column)
        .filter(AssignmentZeroSubmission.user_id.in_(ids))
        .all()
    )
    planned = {r[0]: r[1] for r in rows}
    return {i: planned.get(i) for i in ids}


# --------------------------------------------------------------------------------------
# Official results
# --------------------------------------------------------------------------------------

def latest_results_by_student(
    db: Session,
    student_ids: Sequence[int],
    exam_type: str,
    *,
    cohort_date: Optional[date] = None,
) -> Dict[int, ExamResult]:
    """The result to show as a student's official outcome.

    With ``cohort_date``, returns that specific administration's result - so a grid
    filtered to "the October 3 cohort" compares like with like. Without it, returns
    the most recent non-superseded, non-rejected attempt.
    """
    ids = [i for i in set(student_ids) if i]
    if not ids:
        return {}

    query = (
        db.query(ExamResult)
        .filter(
            ExamResult.student_id.in_(ids),
            ExamResult.exam_type == exam_type,
            ExamResult.is_superseded == False,  # noqa: E712
            ExamResult.status != "rejected",
        )
    )
    if cohort_date is not None:
        query = query.filter(ExamResult.test_date == cohort_date)

    chosen: Dict[int, ExamResult] = {}
    for row in query.order_by(ExamResult.test_date.asc()).all():
        # Ascending order means the last write per student wins = the newest attempt.
        chosen[row.student_id] = row
    return chosen


# --------------------------------------------------------------------------------------
# Bluebook grid
# --------------------------------------------------------------------------------------

def _trend(current: int, previous: Optional[int]) -> tuple[Optional[int], Optional[str]]:
    """Delta and direction vs the previous non-empty column.

    The reference sheet colours these green / red / blue. The UI pairs the trend with
    an arrow glyph so the signal is never conveyed by colour alone.
    """
    if previous is None:
        return None, None
    delta = current - previous
    if delta > 0:
        return delta, "up"
    if delta < 0:
        return delta, "down"
    return 0, "same"


def build_bluebook_grid(
    db: Session,
    group: Group,
    *,
    include_official: bool = True,
    cohort_date: Optional[date] = None,
) -> BluebookGridOut:
    """Assemble the students x chronological-tests grid for one group.

    Columns are one per Bluebook assignment ordered by due date, preceded by the
    Assignment Zero baseline column when any student in the group has one. That
    matches the reference sheet, whose first column is "Bluebook 5" dated at the
    group's start, followed by biweekly "Неделя N" columns.
    """
    student_rows = (
        db.query(UserInDB)
        .join(GroupStudent, GroupStudent.student_id == UserInDB.id)
        .filter(GroupStudent.group_id == group.id, UserInDB.is_trial == False)  # noqa: E712
        .order_by(UserInDB.name)
        .all()
    )
    student_ids = [s.id for s in student_rows]

    results = (
        db.query(BluebookResult)
        .filter(BluebookResult.student_id.in_(student_ids or [-1]))
        .all()
    ) if student_ids else []

    # Bluebook assignments targeted at this group, in column order.
    assignment_ids = {r.assignment_id for r in results if r.assignment_id}
    assignments = (
        db.query(Assignment)
        .filter(Assignment.id.in_(assignment_ids))
        .all()
    ) if assignment_ids else []

    # Assignment -> test number, resolved once instead of scanning `results` per column.
    test_number_by_assignment = {
        r.assignment_id: r.test_number for r in results if r.assignment_id
    }

    def _due(a: Assignment) -> date:
        # Assignments with no due date sort last rather than crashing the sort.
        return a.due_date.date() if a.due_date else date.max

    ordered_assignments = sorted(assignments, key=_due)

    columns: List[BluebookColumn] = []
    has_baseline = any(r.assignment_id is None for r in results)
    if has_baseline:
        columns.append(BluebookColumn(
            key="baseline",
            label="Bluebook 5",
            test_number=5,
            # Assignment Zero stores no date for this score. The reference sheet dates
            # the column at the group's start, so we do the same and mark it a baseline
            # rather than implying we know the day it was taken.
            due_date=group.created_at.date() if group.created_at else None,
            is_baseline=True,
        ))

    for a in ordered_assignments:
        columns.append(BluebookColumn(
            key=f"a{a.id}",
            label=a.title or f"Bluebook",
            test_number=test_number_by_assignment.get(a.id),
            due_date=a.due_date.date() if a.due_date else None,
            week_number=a.lesson_number,
        ))

    by_student: Dict[int, Dict[str, BluebookResult]] = defaultdict(dict)
    for r in results:
        key = "baseline" if r.assignment_id is None else f"a{r.assignment_id}"
        by_student[r.student_id][key] = r

    official = (
        latest_results_by_student(db, student_ids, "sat", cohort_date=cohort_date)
        if include_official and student_ids else {}
    )

    rows: List[BluebookStudentRow] = []
    for student in student_rows:
        cells: Dict[str, BluebookCell] = {}
        previous_total: Optional[int] = None
        totals: List[int] = []
        baseline_total: Optional[int] = None

        for col in columns:
            r = by_student.get(student.id, {}).get(col.key)
            if r is None:
                continue
            delta, trend = _trend(r.total_score, previous_total)
            cells[col.key] = BluebookCell(
                verbal_score=r.verbal_score,
                math_score=r.math_score,
                total_score=r.total_score,
                taken_at=r.taken_at,
                screenshot_url=r.screenshot_url,
                source=r.source,
                delta=delta,
                trend=trend,
            )
            previous_total = r.total_score
            totals.append(r.total_score)
            if col.is_baseline:
                baseline_total = r.total_score

        latest_total = totals[-1] if totals else None
        rows.append(BluebookStudentRow(
            student_id=student.id,
            full_name=(student.official_full_name or student.name or "").strip(),
            display_id=student.student_id,
            cells={k: v.model_dump() for k, v in cells.items()},
            best_total=max(totals) if totals else None,
            latest_total=latest_total,
            improvement_from_baseline=(
                latest_total - baseline_total
                if latest_total is not None and baseline_total is not None else None
            ),
            official_result=(
                ExamResultOut.model_validate(_with_proof_flag(official[student.id]))
                if student.id in official else None
            ),
        ))

    column_stats = _column_statistics(columns, rows, expected_count=len(student_rows))

    teacher = db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first() if group.teacher_id else None

    return BluebookGridOut(
        group_id=group.id,
        group_name=group.name,
        teacher_name=(teacher.official_full_name or teacher.name) if teacher else None,
        start_date=group.created_at.date() if group.created_at else None,
        finish_date=None,
        columns=columns,
        rows=rows,
        column_stats={k: v.model_dump() for k, v in column_stats.items()},
    )


def _with_proof_flag(result: ExamResult) -> ExamResult:
    """Expose only whether proof exists, never the storage key itself.

    Proof of an official exam is a College Board score report - PII that must not be
    handed out in a list payload.
    """
    setattr(result, "has_proof", bool(result.proof_url))
    return result


def _column_statistics(
    columns: Sequence[BluebookColumn],
    rows: Sequence[BluebookStudentRow],
    *,
    expected_count: int,
) -> Dict[str, BluebookColumnStats]:
    """Per-column aggregates.

    Completion rate excludes the Assignment Zero baseline column: it is not homework,
    so counting it against a group's homework completion would misreport the group.
    """
    stats: Dict[str, BluebookColumnStats] = {}
    for col in columns:
        totals: List[int] = []
        verbals: List[int] = []
        maths: List[int] = []
        for row in rows:
            cell = row.cells.get(col.key)
            if not cell:
                continue
            totals.append(cell["total_score"])
            verbals.append(cell["verbal_score"])
            maths.append(cell["math_score"])

        submitted = len(totals)
        stats[col.key] = BluebookColumnStats(
            submitted_count=submitted,
            expected_count=0 if col.is_baseline else expected_count,
            completion_rate=(
                0.0 if col.is_baseline or expected_count == 0
                else round(submitted / expected_count, 4)
            ),
            mean_total=round(mean(totals), 1) if totals else None,
            median_total=round(median(totals), 1) if totals else None,
            mean_verbal=round(mean(verbals), 1) if verbals else None,
            mean_math=round(mean(maths), 1) if maths else None,
        )
    return stats
