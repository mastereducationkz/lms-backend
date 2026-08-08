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

            # Scores come ONLY from the official College Board PDF, re-parsed here from
            # the stored file. The client sends a storage key, never a score, so a
            # tampered submission payload cannot change what is recorded.
            report_key = answer.get("report_key")
            if not isinstance(report_key, str) or not report_key:
                # No official report: nothing to record. Manual entry is no longer
                # accepted for Bluebook.
                continue

            parsed = _parse_stored_report(report_key)
            if parsed is None:
                continue

            verbal = parsed.verbal_score
            math = parsed.math_score

            task_content = task.get("content") or {}
            test_number = task_content.get("test_number")
            if not isinstance(test_number, int) or isinstance(test_number, bool):
                continue
            if not (BLUEBOOK_MIN_TEST_NUMBER <= test_number <= BLUEBOOK_MAX_TEST_NUMBER):
                continue
            # The report must be for the test that was actually assigned.
            if parsed.test_number != test_number:
                logger.warning(
                    "Bluebook report is for test %s but assignment %s asks for %s; skipping",
                    parsed.test_number, assignment.id, test_number,
                )
                continue

            screenshot_url = _first_file_url(answer)
            taken_at = parsed.report_date or _parse_date(answer.get("taken_at"))

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
            # Provenance: which file the scores came from, and whose name is on it.
            existing.report_url = report_key
            existing.report_student_name = parsed.student_name
            existing.report_date = parsed.report_date
            existing.report_name_matches = _name_matches(parsed.student_name, submission)
            written += 1
    except Exception:  # pragma: no cover - defensive
        logger.exception("Bluebook projection failed for submission %s",
                         getattr(submission, "id", None))
        return 0
    return written


def _parse_stored_report(report_key: str):
    """Re-parse the stored PDF. Returns None if it is missing or unreadable."""
    from src.exams.bluebook_pdf import BluebookReportError, parse_report_pdf
    from src.services import storage_service

    data = storage_service.read(report_key)
    if not data:
        logger.warning("Bluebook report missing from storage: %s", report_key)
        return None
    try:
        return parse_report_pdf(data)
    except BluebookReportError as exc:
        logger.warning("Stored Bluebook report failed to re-parse (%s): %s", report_key, exc)
        return None


def _name_matches(pdf_name: Optional[str], submission) -> Optional[bool]:
    """Flag, never block - see bluebook_pdf.names_are_similar."""
    from src.exams.bluebook_pdf import names_are_similar

    user = getattr(submission, "user", None)
    account_name = (getattr(user, "official_full_name", None) or getattr(user, "name", None)) if user else None
    if not pdf_name or not account_name:
        return None
    return names_are_similar(pdf_name, account_name)


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
