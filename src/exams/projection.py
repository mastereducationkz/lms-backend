"""Write the queryable Bluebook projection at submission time.

The submission of record stays in ``assignment_submissions.answers``. This module
mirrors the Bluebook parts of it into ``bluebook_results`` so the group grid and its
statistics can aggregate in SQL.

Why a projection rather than reading the JSON directly: ``answers`` is a ``Text``
column (not JSONB), so it cannot be indexed as JSON, and its keys are random
``task_<timestamp>_<random>`` ids minted per assignment, so there is no stable path to
a given task's score. Building a students x tests grid from it would be a full scan
plus per-row JSON parsing.

Failures here must never fail the student's submission - the submission itself is
already durably stored. Projection errors are logged and swallowed.
"""
import logging
from datetime import date
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from src.exams.models import (
    BLUEBOOK_MAX_TEST_NUMBER,
    BLUEBOOK_MIN_TEST_NUMBER,
    BluebookResult,
)

logger = logging.getLogger(__name__)

SAT_SECTION_MIN = 200
SAT_SECTION_MAX = 800


def _coerce_section(value: Any) -> Optional[int]:
    """Accept an int or a numeric string; reject anything outside a valid SAT section."""
    if value is None or isinstance(value, bool):
        return None
    try:
        score = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not (SAT_SECTION_MIN <= score <= SAT_SECTION_MAX):
        return None
    if score % 10 != 0:
        return None
    return score


def _iter_bluebook_tasks(content: Dict[str, Any]):
    """Yield (task_id, task) for every bluebook_task defined on the assignment."""
    tasks = (content or {}).get("tasks")
    if not isinstance(tasks, list):
        return
    for task in tasks:
        if isinstance(task, dict) and task.get("task_type") == "bluebook_task":
            yield task.get("id"), task


def project_bluebook_answers(
    db: Session,
    *,
    assignment,
    submission,
    answers: Dict[str, Any],
    assignment_content: Dict[str, Any],
) -> int:
    """Upsert one ``bluebook_results`` row per bluebook_task in this submission.

    Returns the number of rows written. Never raises: a projection problem must not
    cost a student their submission.
    """
    written = 0
    try:
        for task_id, task in _iter_bluebook_tasks(assignment_content):
            if not task_id:
                continue
            answer = (answers or {}).get(task_id)
            if not isinstance(answer, dict):
                continue

            verbal = _coerce_section(answer.get("verbal_score"))
            math = _coerce_section(answer.get("math_score"))
            if verbal is None or math is None:
                # Incomplete or invalid: leave the cell empty rather than storing a
                # half-row that would distort the group's averages.
                continue

            task_content = task.get("content") or {}
            test_number = task_content.get("test_number")
            if not isinstance(test_number, int) or isinstance(test_number, bool):
                continue
            if not (BLUEBOOK_MIN_TEST_NUMBER <= test_number <= BLUEBOOK_MAX_TEST_NUMBER):
                continue

            screenshot_url = _first_file_url(answer)
            taken_at = _parse_date(answer.get("taken_at"))

            existing = (
                db.query(BluebookResult)
                .filter(
                    BluebookResult.student_id == submission.user_id,
                    BluebookResult.assignment_id == assignment.id,
                )
                .first()
            )
            if existing is None:
                existing = BluebookResult(
                    student_id=submission.user_id,
                    assignment_id=assignment.id,
                    source="homework",
                )
                db.add(existing)

            existing.submission_id = submission.id
            existing.group_id = assignment.group_id
            existing.test_number = test_number
            existing.verbal_score = verbal
            existing.math_score = math
            # Always derived. The reference sheet defines Score = Verbal + Math, and a
            # client-supplied total could contradict its own sections.
            existing.total_score = verbal + math
            existing.screenshot_url = screenshot_url
            existing.taken_at = taken_at or (
                assignment.due_date.date() if assignment.due_date else None
            )
            written += 1
    except Exception:  # pragma: no cover - defensive
        logger.exception("Bluebook projection failed for submission %s",
                         getattr(submission, "id", None))
        return 0
    return written


def _first_file_url(answer: Dict[str, Any]) -> Optional[str]:
    """The screenshot, from either the multi-file or the legacy single-file shape."""
    files = answer.get("files")
    if isinstance(files, list):
        for f in files:
            if isinstance(f, dict) and f.get("file_url"):
                return f["file_url"]
    url = answer.get("file_url")
    return url if isinstance(url, str) and url else None


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
