#!/usr/bin/env python3
"""
Cleanup incorrect SAT planned dates that ended up in the past.

This targets rows where:
- group is not IELTS-only
- SAT result isn't filled yet
- sat_planned_test_date < today

By default, this sets sat_planned_test_date to NULL (so it doesn't create wrong "ask result" tasks).
Dry-run unless --apply is passed.
"""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import SessionLocal
from src.assignments.models import AssignmentZeroSubmission


def parse_args() -> ArgumentParser:
    p = ArgumentParser(description="Cleanup past sat_planned_test_date values")
    p.add_argument("--apply", action="store_true", help="Commit changes to DB")
    p.add_argument("--limit", type=int, default=0, help="Optional limit for processing (0 = all)")
    return p


def is_ielts_only_group(group_name: str | None) -> bool:
    name = (group_name or "").lower()
    return "ielts" in name and "sat" not in name


def has_sat_result(row: AssignmentZeroSubmission) -> bool:
    return bool(row.sat_result_score and row.sat_result_test_date)


def main() -> None:
    args = parse_args().parse_args()
    today = date.today()

    db = SessionLocal()
    try:
        q = db.query(AssignmentZeroSubmission).filter(AssignmentZeroSubmission.sat_planned_test_date.isnot(None))
        if args.limit and args.limit > 0:
            q = q.limit(args.limit)
        rows = q.all()

        considered = 0
        skipped_ielts = 0
        skipped_has_result = 0
        skipped_not_past = 0
        will_null = []

        for row in rows:
            considered += 1
            if is_ielts_only_group(row.group_name):
                skipped_ielts += 1
                continue
            if has_sat_result(row):
                skipped_has_result += 1
                continue
            if row.sat_planned_test_date >= today:
                skipped_not_past += 1
                continue

            will_null.append((row.user_id, row.sat_planned_test_date, row.sat_target_date))
            row.sat_planned_test_date = None

        print(f"considered={considered}")
        print(f"skipped_ielts_only={skipped_ielts}")
        print(f"skipped_has_sat_result={skipped_has_result}")
        print(f"skipped_not_past={skipped_not_past}")
        print(f"will_null={len(will_null)}")

        for user_id, before, target in will_null[:10]:
            print(f"example user_id={user_id} target='{target}' planned_before={before} planned_after=None")

        if not args.apply:
            print("Dry-run mode: no changes written. Re-run with --apply to commit.")
            db.rollback()
            return

        db.commit()
        print(f"applied_nulls={len(will_null)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

