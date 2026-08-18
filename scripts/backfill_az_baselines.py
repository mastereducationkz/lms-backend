"""Backfill Bluebook-5 baseline rows for Assignment Zero submissions.

The 2026-08-08 one-off backfill created ``bluebook_results`` baseline rows
(``assignment_id IS NULL``) for the AZ submissions that existed then; nothing kept
them in sync afterwards. This script replays the ongoing-sync helper
(:func:`src.exams.baseline.record_assignment_zero_baseline`) for every non-draft AZ
submission that still has no baseline row — safe to re-run, skips overridden rows
and unparseable/empty answers.

Run inside the backend container:
    python -m scripts.backfill_az_baselines           # dry run (default)
    python -m scripts.backfill_az_baselines --apply   # write
"""
from __future__ import annotations

import argparse

from src.config import SessionLocal
from src.exams.baseline import parse_bluebook5_score, record_assignment_zero_baseline
from src.exams.models import BluebookResult
from src.schemas.models import AssignmentZeroSubmission


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write rows (default: dry run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        with_baseline = {
            row[0]
            for row in db.query(BluebookResult.student_id)
            .filter(BluebookResult.assignment_id.is_(None))
            .all()
        }
        submissions = (
            db.query(AssignmentZeroSubmission)
            .filter(AssignmentZeroSubmission.is_draft == False)  # noqa: E712
            .all()
        )

        written = skipped_has_row = skipped_unparseable = 0
        for sub in submissions:
            if sub.user_id in with_baseline:
                skipped_has_row += 1
                continue
            if parse_bluebook5_score(sub.bluebook_practice_test_5_score) is None:
                skipped_unparseable += 1
                continue
            if args.apply:
                record_assignment_zero_baseline(
                    db, sub.user_id, sub.bluebook_practice_test_5_score,
                    screenshot_url=sub.screenshot_url,
                )
            written += 1

        if args.apply:
            db.commit()
        print(
            f"{'WROTE' if args.apply else 'WOULD WRITE'} {written} baselines; "
            f"already had {skipped_has_row}; unparseable/empty {skipped_unparseable}; "
            f"total non-draft submissions {len(submissions)}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
