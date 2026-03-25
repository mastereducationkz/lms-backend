#!/usr/bin/env python3
"""
Recalculate and sync groups.is_over based on class schedule completion.

Rules:
- group is over when all planned class lessons are in the past
- and there are no future class lessons

Safe by default:
- Runs in dry-run mode unless --apply is provided.
"""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from src.config import SessionLocal
from src.services.group_completion_service import get_groups_over_status_changes


def parse_args() -> ArgumentParser:
    parser = ArgumentParser(description="Sync groups.is_over by schedule completion")
    parser.add_argument(
        "--group-ids",
        default="",
        help="Optional comma-separated group ids (example: 4,17,29)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to DB. Without this flag script runs in dry-run mode.",
    )
    return parser


def parse_group_ids(raw_group_ids: str) -> list[int]:
    if not raw_group_ids.strip():
        return []

    result: list[int] = []
    for chunk in raw_group_ids.split(","):
        value = chunk.strip()
        if value:
            result.append(int(value))
    return result


def main() -> None:
    args = parse_args().parse_args()
    group_ids = parse_group_ids(args.group_ids)

    db = SessionLocal()
    try:
        changes = get_groups_over_status_changes(db, group_ids if group_ids else None)
        print(f"changes_found={len(changes)}")

        for group, target_state in changes[:30]:
            print(f"group_id={group.id} name={group.name} is_over={group.is_over} -> {target_state}")

        if not args.apply:
            print("Dry-run mode: no changes written. Re-run with --apply to commit.")
            db.rollback()
            return

        for group, target_state in changes:
            group.is_over = target_state

        db.commit()
        print(f"applied_updates={len(changes)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
