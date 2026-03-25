#!/usr/bin/env python3
"""
Mark groups as finished (`is_over = true`) in bulk.

Safe by default:
- Runs in dry-run mode unless `--apply` is provided.

Ways to select groups:
1) By explicit ids: --group-ids 12,34,56
2) By CSV file with `group_id` column: --csv backend/reports/finished_groups.csv
   - If `is_finished_by_program` column exists, only rows with truthy value are used
     unless `--use-all-csv-rows` is passed.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import csv
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import SessionLocal
from src.courses.models import Group


TRUTHY_VALUES = {"1", "true", "yes", "y", "t"}
SCRIPT_PATH = Path(__file__).resolve()
BACKEND_ROOT = SCRIPT_PATH.parents[1]
PROJECT_ROOT = SCRIPT_PATH.parents[2]


def parse_args() -> ArgumentParser:
    parser = ArgumentParser(description="Mark groups as finished (is_over=true)")
    parser.add_argument(
        "--group-ids",
        default="",
        help="Comma-separated group ids (example: 12,34,56)",
    )
    parser.add_argument(
        "--csv",
        default="backend/reports/finished_groups.csv",
        help="Path to CSV with at least `group_id` column",
    )
    parser.add_argument(
        "--use-all-csv-rows",
        action="store_true",
        help="Use all rows from CSV, ignore `is_finished_by_program` filter",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to DB. Without this flag script runs in dry-run mode.",
    )
    return parser


def resolve_csv_path(raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate

    # Try path relative to current working directory first
    if candidate.exists():
        return candidate

    # Then try project root (works for default backend/reports/... path)
    project_candidate = PROJECT_ROOT / candidate
    if project_candidate.exists():
        return project_candidate

    # Finally try backend root for cases like --csv reports/finished_groups.csv
    backend_candidate = BACKEND_ROOT / candidate
    if backend_candidate.exists():
        return backend_candidate

    # Fallback to project-root-relative path for consistent diagnostics
    return project_candidate


def parse_group_ids_from_arg(raw_group_ids: str) -> set[int]:
    if not raw_group_ids.strip():
        return set()

    result: set[int] = set()
    for chunk in raw_group_ids.split(","):
        value = chunk.strip()
        if not value:
            continue
        result.add(int(value))
    return result


def parse_group_ids_from_csv(csv_path: Path, use_all_rows: bool) -> set[int]:
    if not csv_path.exists():
        return set()

    with csv_path.open("r", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or "group_id" not in reader.fieldnames:
            raise ValueError("CSV must contain `group_id` column")

        result: set[int] = set()
        has_finished_column = "is_finished_by_program" in reader.fieldnames

        for row in reader:
            group_id_raw = (row.get("group_id") or "").strip()
            if not group_id_raw:
                continue

            if not use_all_rows and has_finished_column:
                finished_raw = (row.get("is_finished_by_program") or "").strip().lower()
                if finished_raw not in TRUTHY_VALUES:
                    continue

            result.add(int(group_id_raw))

    return result


def main() -> None:
    args = parse_args().parse_args()
    csv_path = resolve_csv_path(args.csv)

    ids_from_args = parse_group_ids_from_arg(args.group_ids)
    ids_from_csv = parse_group_ids_from_csv(csv_path, args.use_all_csv_rows)
    target_group_ids = sorted(ids_from_args | ids_from_csv)

    print(f"ids_from_args={len(ids_from_args)}")
    print(f"ids_from_csv={len(ids_from_csv)} (csv_exists={csv_path.exists()})")
    print(f"target_total={len(target_group_ids)}")

    if not target_group_ids:
        print("Nothing to update. Pass --group-ids or valid --csv data.")
        return

    db = SessionLocal()
    try:
        groups = db.query(Group).filter(Group.id.in_(target_group_ids)).all()
        found_ids = {group.id for group in groups}
        missing_ids = [group_id for group_id in target_group_ids if group_id not in found_ids]

        to_mark_over = [group for group in groups if not group.is_over]
        already_over = [group for group in groups if group.is_over]

        print(f"found_in_db={len(groups)}")
        print(f"missing_in_db={len(missing_ids)}")
        if missing_ids:
            print(f"missing_ids={missing_ids[:20]}")

        print(f"will_mark_over={len(to_mark_over)}")
        print(f"already_over={len(already_over)}")

        for group in to_mark_over[:20]:
            print(f"example update: id={group.id}, name={group.name}, is_over={group.is_over} -> True")

        if not args.apply:
            print("Dry-run mode: no changes written. Re-run with --apply to commit.")
            db.rollback()
            return

        for group in to_mark_over:
            group.is_over = True

        db.commit()
        print(f"applied_updates={len(to_mark_over)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
