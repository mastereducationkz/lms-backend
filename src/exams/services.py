"""Exam-results domain services.

Contact resolution and grid assembly both run over whole groups, so every lookup here
is batched. A per-student query would turn the Bluebook grid into an N+1 across every
row on every render.
"""
import json
from collections import defaultdict
from datetime import date
from statistics import mean, median
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from src.assignments.models import Assignment, AssignmentZeroSubmission
from src.auth.models import UserInDB
from src.courses.models import Group, GroupStudent
from src.exams.models import (
    BLUEBOOK_MAX_TEST_NUMBER,
    BLUEBOOK_MIN_TEST_NUMBER,
    BluebookResult,
    ExamResult,
)
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


def _bluebook_assignments_for_group(db: Session, group_id: int) -> Dict[int, Assignment]:
    """Map test number -> the assignment that assigned it to this group.

    Bluebook tasks live inside ``multi_task`` content, which is a JSON blob in a Text
    column, so the test number cannot be filtered in SQL. The LIKE narrows the scan to
    candidate rows before parsing; a group has tens of assignments, not thousands.

    When the same test is assigned more than once, the LATEST assignment wins, so the
    grid shows the most recent attempt for that test rather than a stale one.
    """
    candidates = (
        db.query(Assignment)
        .filter(
            Assignment.group_id == group_id,
            Assignment.assignment_type == "multi_task",
            Assignment.is_active == True,  # noqa: E712
            Assignment.content.like("%bluebook_task%"),
        )
        .all()
    )

    def _due_key(a: Assignment) -> date:
        return a.due_date.date() if a.due_date else date.min

    by_test: Dict[int, Assignment] = {}
    for assignment in sorted(candidates, key=_due_key):
        try:
            content = json.loads(assignment.content) if assignment.content else {}
        except (TypeError, ValueError):
            continue
        tasks = content.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict) or task.get("task_type") != "bluebook_task":
                continue
            number = (task.get("content") or {}).get("test_number")
            if isinstance(number, int) and not isinstance(number, bool):
                if BLUEBOOK_MIN_TEST_NUMBER <= number <= BLUEBOOK_MAX_TEST_NUMBER:
                    by_test[number] = assignment  # later due date overwrites
    return by_test


def build_bluebook_grid(
    db: Session,
    group: Group,
    *,
    include_official: bool = True,
    cohort_date: Optional[date] = None,
) -> BluebookGridOut:
    """Assemble the students x Bluebook-tests grid for one group.

    Every Bluebook test 4-11 is always rendered as a column, whether or not it has been
    assigned, so staff can see coverage at a glance. Each column reports its own state:

    ``assigned``      a Bluebook homework exists for that test in this group
    ``not_assigned``  no homework has been created for it yet

    and each cell distinguishes a real score from "assigned but not submitted" and from
    "never assigned". Collapsing those three into an empty cell hides whether the gap is
    the student's or the teacher's.

    The Assignment Zero baseline ("Bluebook 5") is prepended when any student has one.
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
        .filter(BluebookResult.student_id.in_(student_ids))
        .all()
    ) if student_ids else []

    assignment_by_test = _bluebook_assignments_for_group(db, group.id)

    # ---- columns: baseline (if present) then every test 4-11 -----------------------
    columns: List[BluebookColumn] = []
    has_baseline = any(r.assignment_id is None for r in results)
    if has_baseline:
        columns.append(BluebookColumn(
            key="baseline",
            label="Bluebook 5 (baseline)",
            test_number=5,
            # Assignment Zero records no date for this score; the reference sheet dates
            # it at the group's start, so we do the same and mark it a baseline rather
            # than implying we know the day it was taken.
            due_date=group.created_at.date() if group.created_at else None,
            is_baseline=True,
            is_assigned=True,
        ))

    for number in range(BLUEBOOK_MIN_TEST_NUMBER, BLUEBOOK_MAX_TEST_NUMBER + 1):
        assignment = assignment_by_test.get(number)
        columns.append(BluebookColumn(
            key=f"t{number}",
            label=f"Bluebook #{number}",
            test_number=number,
            assignment_id=assignment.id if assignment else None,
            due_date=assignment.due_date.date() if (assignment and assignment.due_date) else None,
            week_number=assignment.lesson_number if assignment else None,
            is_baseline=False,
            is_assigned=assignment is not None,
        ))

    # ---- index results by (student, column) ----------------------------------------
    assignment_id_to_test = {a.id: n for n, a in assignment_by_test.items()}
    by_student: Dict[int, Dict[str, BluebookResult]] = defaultdict(dict)
    for r in results:
        if r.assignment_id is None:
            key = "baseline"
        else:
            # Prefer the column the assignment maps to; fall back to the number stored
            # on the row so a result still lands somewhere if its assignment was
            # deleted or edited to a different test.
            number = assignment_id_to_test.get(r.assignment_id, r.test_number)
            key = f"t{number}"
        # A later submission for the same cell wins.
        existing = by_student[r.student_id].get(key)
        if existing is None or (r.updated_at or r.created_at or 0) >= (existing.updated_at or existing.created_at or 0):
            by_student[r.student_id][key] = r

    official = (
        latest_results_by_student(db, student_ids, "sat", cohort_date=cohort_date)
        if include_official and student_ids else {}
    )

    # ---- rows -----------------------------------------------------------------------
    rows: List[BluebookStudentRow] = []
    for student in student_rows:
        cells: Dict[str, BluebookCell] = {}
        previous_total: Optional[int] = None
        totals: List[int] = []
        baseline_total: Optional[int] = None

        for col in columns:
            r = by_student.get(student.id, {}).get(col.key)
            if r is None:
                # Distinguish "we never asked" from "asked and got nothing".
                cells[col.key] = BluebookCell(
                    state="not_assigned" if not col.is_assigned else "not_submitted",
                )
                continue
            delta, trend = _trend(r.total_score, previous_total)
            cells[col.key] = BluebookCell(
                state="submitted",
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
        assigned_non_baseline = [c for c in columns if c.is_assigned and not c.is_baseline]
        submitted_non_baseline = sum(
            1 for c in assigned_non_baseline if cells[c.key].state == "submitted"
        )
        official_row = official.get(student.id)

        rows.append(BluebookStudentRow(
            student_id=student.id,
            full_name=(student.official_full_name or student.name or "").strip(),
            display_id=student.student_id,
            email=student.email,
            cells={k: v.model_dump() for k, v in cells.items()},
            submitted_count=submitted_non_baseline,
            assigned_count=len(assigned_non_baseline),
            best_total=max(totals) if totals else None,
            latest_total=latest_total,
            average_total=round(mean(totals), 1) if totals else None,
            baseline_total=baseline_total,
            improvement_from_baseline=(
                latest_total - baseline_total
                if latest_total is not None and baseline_total is not None else None
            ),
            official_result=(
                ExamResultOut.model_validate(_with_proof_flag(official_row))
                if official_row else None
            ),
        ))

    column_stats = _column_statistics(columns, rows, expected_count=len(student_rows))
    teacher = (
        db.query(UserInDB).filter(UserInDB.id == group.teacher_id).first()
        if group.teacher_id else None
    )

    return BluebookGridOut(
        group_id=group.id,
        group_name=group.name,
        teacher_name=(teacher.official_full_name or teacher.name) if teacher else None,
        start_date=group.created_at.date() if group.created_at else None,
        finish_date=None,
        columns=columns,
        rows=rows,
        column_stats={k: v.model_dump() for k, v in column_stats.items()},
        group_stats=_group_statistics(columns, rows).model_dump(),
    )


def _group_statistics(
    columns: Sequence[BluebookColumn],
    rows: Sequence[BluebookStudentRow],
) -> "BluebookGroupStats":
    """Whole-group aggregates.

    Completion counts only assigned, non-baseline columns: the Assignment Zero baseline
    is diagnostic data, not homework, and tests nobody was asked to sit must not be
    scored against the group.
    """
    from src.exams.schemas import BluebookGroupStats

    assigned = [c for c in columns if c.is_assigned and not c.is_baseline]
    expected = len(assigned) * len(rows)
    submitted = sum(r.submitted_count for r in rows)

    latest_totals = [r.latest_total for r in rows if r.latest_total is not None]
    best_totals = [r.best_total for r in rows if r.best_total is not None]
    improvements = [
        r.improvement_from_baseline for r in rows if r.improvement_from_baseline is not None
    ]

    return BluebookGroupStats(
        student_count=len(rows),
        tests_assigned=len(assigned),
        tests_available=sum(1 for c in columns if not c.is_baseline),
        submitted_count=submitted,
        expected_count=expected,
        completion_rate=round(submitted / expected, 4) if expected else 0.0,
        average_latest_total=round(mean(latest_totals), 1) if latest_totals else None,
        median_latest_total=round(median(latest_totals), 1) if latest_totals else None,
        average_best_total=round(mean(best_totals), 1) if best_totals else None,
        highest_total=max(best_totals) if best_totals else None,
        lowest_latest_total=min(latest_totals) if latest_totals else None,
        average_improvement=round(mean(improvements), 1) if improvements else None,
        improved_count=sum(1 for i in improvements if i > 0),
        declined_count=sum(1 for i in improvements if i < 0),
        students_with_no_results=sum(1 for r in rows if r.latest_total is None),
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
            # Only cells with a real submission contribute to averages; a
            # not_submitted / not_assigned cell carries no scores.
            if not cell or cell.get("state") != "submitted":
                continue
            totals.append(cell["total_score"])
            verbals.append(cell["verbal_score"])
            maths.append(cell["math_score"])

        submitted = len(totals)
        # An unassigned test has no expectation attached to it, so it is not scored
        # against the group's completion.
        counts_toward_completion = col.is_assigned and not col.is_baseline
        stats[col.key] = BluebookColumnStats(
            submitted_count=submitted,
            expected_count=expected_count if counts_toward_completion else 0,
            completion_rate=(
                round(submitted / expected_count, 4)
                if counts_toward_completion and expected_count else 0.0
            ),
            mean_total=round(mean(totals), 1) if totals else None,
            median_total=round(median(totals), 1) if totals else None,
            mean_verbal=round(mean(verbals), 1) if verbals else None,
            mean_math=round(mean(maths), 1) if maths else None,
        )
    return stats
