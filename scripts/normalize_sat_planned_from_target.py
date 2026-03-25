#!/usr/bin/env python3
"""
Normalize SAT planned dates using sat_target_date.

Purpose:
- Undo incorrect mass backfills where sat_planned_test_date was set to a fixed date (e.g. March)
  even though sat_target_date points to a different exam month/day.

Rules:
- Skip IELTS-only groups (group_name contains 'ielts' and not 'sat')
- Skip rows that already have SAT result (score + test_date)
- If sat_target_date can be parsed to a date:
    - update sat_planned_test_date when it's missing OR differs from parsed target date

Safe by default:
- Dry-run unless --apply is provided.
"""

from __future__ import annotations

from argparse import ArgumentParser
from datetime import date
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import SessionLocal
from src.assignments.models import AssignmentZeroSubmission
from src.assignments.exam_dates import parse_sat_target_date

from datetime import datetime


def parse_args() -> ArgumentParser:
    p = ArgumentParser(description="Normalize sat_planned_test_date from sat_target_date")
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
        q = db.query(AssignmentZeroSubmission)
        if args.limit and args.limit > 0:
            q = q.limit(args.limit)
        rows = q.all()

        considered = 0
        skipped_ielts = 0
        skipped_has_result = 0
        skipped_unparseable = 0
        to_update = []

        for row in rows:
            considered += 1
            if is_ielts_only_group(row.group_name):
                skipped_ielts += 1
                continue
            if has_sat_result(row):
                skipped_has_result += 1
                continue

            target_raw = (row.sat_target_date or "").strip()
            if not target_raw:
                skipped_unparseable += 1
                continue

            parsed = parse_sat_target_date(target_raw, reference_date=today)

            if not parsed:
                skipped_unparseable += 1
                continue

            if row.sat_planned_test_date != parsed:
                to_update.append((row.user_id, row.sat_planned_test_date, parsed, row.sat_target_date))
                row.sat_planned_test_date = parsed

        print(f"considered={considered}")
        print(f"skipped_ielts_only={skipped_ielts}")
        print(f"skipped_has_sat_result={skipped_has_result}")
        print(f"skipped_unparseable_target={skipped_unparseable}")
        print(f"will_update={len(to_update)}")

        for user_id, before, after, target in to_update[:10]:
            print(f"example user_id={user_id} target='{target}' planned_before={before} planned_after={after}")

        if not args.apply:
            print("Dry-run mode: no changes written. Re-run with --apply to commit.")
            db.rollback()
            return

        db.commit()
        print(f"applied_updates={len(to_update)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

