"""Assignment Zero → Bluebook-5 baseline projection.

The Bluebook group grid reads baselines from ``bluebook_results`` rows with
``assignment_id IS NULL``. Historically those rows came from a one-off backfill
(2026-08-08) that parsed ``assignment_zero_submissions.bluebook_practice_test_5_score``;
nothing kept them in sync afterwards, so every student who submitted Assignment Zero
after that date had a score in the questionnaire and an empty grid («Бейслайна нет»).

This module is the ongoing sync: the AZ submit endpoint calls
:func:`record_assignment_zero_baseline` in the same transaction, and
``scripts/backfill_az_baselines.py`` replays it for historical rows.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from src.exams.models import BluebookResult

_MATH_RE = re.compile(r"math\s*[:\-]?\s*(\d{1,3})", re.IGNORECASE)
_VERBAL_RE = re.compile(r"verbal\s*[:\-]?\s*(\d{1,3})", re.IGNORECASE)

#: A Bluebook section score is 200-800, but the AZ form historically accepted 0 for
#: "did not take it" and the 2026-08-08 backfill kept those rows (0/0/0), so the grid
#: could distinguish "took it and bombed" from "never asked". We keep that parity.
_MAX_SECTION_SCORE = 800


def parse_bluebook5_score(raw: Optional[str]) -> Optional[Tuple[int, int]]:
    """``"Math 440, Verbal 410"`` → ``(verbal, math)``; None when unparseable.

    The AZ form writes the string itself, so in practice the format is uniform —
    but the field is free text in the schema, so junk must not raise.
    """
    if not raw:
        return None
    math_m = _MATH_RE.search(raw)
    verbal_m = _VERBAL_RE.search(raw)
    if not math_m or not verbal_m:
        return None
    math, verbal = int(math_m.group(1)), int(verbal_m.group(1))
    if math > _MAX_SECTION_SCORE or verbal > _MAX_SECTION_SCORE:
        return None
    return verbal, math


def record_assignment_zero_baseline(
    db: Session,
    student_id: int,
    raw_score: Optional[str],
    screenshot_url: Optional[str] = None,
) -> bool:
    """Upsert the student's baseline row from their AZ Bluebook-5 answer.

    Does not commit — the caller owns the transaction. Returns True when a row was
    written/updated, False when the answer was empty or unparseable (no row: the grid
    must keep showing the absence rather than a fabricated zero).
    """
    parsed = parse_bluebook5_score(raw_score)
    if parsed is None:
        return False
    verbal, math = parsed

    row = (
        db.query(BluebookResult)
        .filter(
            BluebookResult.student_id == student_id,
            BluebookResult.assignment_id.is_(None),
        )
        .first()
    )
    if row is None:
        row = BluebookResult(
            student_id=student_id,
            assignment_id=None,
            submission_id=None,
            group_id=None,
            test_number=5,
            verbal_score=verbal,
            math_score=math,
            total_score=verbal + math,
            screenshot_url=screenshot_url,
            taken_at=None,
            source="assignment_zero",
        )
        db.add(row)
        return True

    # A manually corrected baseline (staff override) must not be silently clobbered
    # by a re-submitted questionnaire.
    if row.overridden_by is not None:
        return False
    row.verbal_score = verbal
    row.math_score = math
    row.total_score = verbal + math
    if screenshot_url:
        row.screenshot_url = screenshot_url
    row.updated_at = datetime.now(timezone.utc)
    return True
