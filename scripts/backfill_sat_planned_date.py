#!/usr/bin/env python3
"""
Backfill SAT planned date for Assignment Zero submissions.

Rule:
- Update rows where SAT info is missing, or
- SAT follow-up is overdue (planned_date + 13 days passed, but SAT result is incomplete)

Default target planned test date is 2026-03-14, which means Ask result date = 2026-03-27.

Safe by default:
- Runs in dry-run mode unless --apply is provided.
"""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import date, timedelta
from pathlib import Path
import sys

# Allow running as: python scripts/backfill_sat_planned_date.py
sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import SessionLocal
from src.assignments.models import AssignmentZeroSubmission


def parse_args() -> ArgumentParser:
    parser = ArgumentParser(description="Backfill SAT planned test date")
    parser.add_argument(
        "--target-date",
        default="2026-03-14",
        help="SAT planned test date in YYYY-MM-DD format (default: 2026-03-14)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to DB. Without this flag script runs in dry-run mode.",
    )
    return parser


def is_incomplete_sat(row: AssignmentZeroSubmission) -> bool:
    # "Incomplete" here means we don't know WHEN the SAT is planned,
    # not that the result is missing (results are naturally missing before the exam).
    return row.sat_planned_test_date is None


def is_ielts_only_group(row: AssignmentZeroSubmission) -> bool:
    group_name = (row.group_name or "").lower()
    return "ielts" in group_name


def is_overdue_without_result(row: AssignmentZeroSubmission, today: date) -> bool:
    if row.sat_planned_test_date is None:
        return False
    ask_date = row.sat_planned_test_date + timedelta(days=13)
    has_complete_result = bool(row.sat_result_score and row.sat_result_test_date)
    return ask_date < today and not has_complete_result


def main() -> None:
    args = parse_args().parse_args()
    target_date = date.fromisoformat(args.target_date)
    ask_result_date = target_date + timedelta(days=13)
    today = date.today()

    db = SessionLocal()
    try:
        rows = db.query(AssignmentZeroSubmission).all()
        to_update = []

        for row in rows:
            if is_ielts_only_group(row):
                continue
        if is_incomplete_sat(row) or is_overdue_without_result(row, today):
                if row.sat_planned_test_date != target_date:
                    to_update.append(row)

        print(f"Found rows to update: {len(to_update)}")
        print(f"Target SAT planned date: {target_date.isoformat()}")
        print(f"Derived Ask result date: {ask_result_date.isoformat()}")

        if not args.apply:
            print("Dry-run mode: no changes written. Re-run with --apply to commit.")
            return

        for row in to_update:
            row.sat_planned_test_date = target_date

        db.commit()
        print(f"Applied updates: {len(to_update)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

