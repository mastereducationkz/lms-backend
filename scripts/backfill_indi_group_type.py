#!/usr/bin/env python3
"""
Set group_type = 'individual' for all groups whose name contains 'indi' (case-insensitive).

Safe by default:
- Runs in dry-run mode unless --apply is provided.
- Pass --group-ids to restrict to specific groups only.
"""

from __future__ import annotations

import sys
from argparse import ArgumentParser
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func

from src.config import SessionLocal
from src.courses.models import Group


def parse_args() -> ArgumentParser:
    parser = ArgumentParser(
        description="Backfill group_type='individual' for groups whose name contains 'indi'"
    )
    parser.add_argument(
        "--group-ids",
        default="",
        help="Optional comma-separated group ids to restrict (example: 4,17,29). "
             "If omitted, all groups with 'indi' in their name are targeted.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to DB. Without this flag the script runs in dry-run mode.",
    )
    return parser


def parse_group_ids(raw: str) -> list[int]:
    if not raw.strip():
        return []
    return [int(chunk.strip()) for chunk in raw.split(",") if chunk.strip()]


def main() -> None:
    args = parse_args().parse_args()
    explicit_ids = parse_group_ids(args.group_ids)

    db = SessionLocal()
    try:
        query = db.query(Group).filter(
            func.lower(Group.name).contains("indi"),
            Group.group_type != "individual",
        )

        if explicit_ids:
            query = query.filter(Group.id.in_(explicit_ids))

        groups = query.all()

        print(f"groups_to_update={len(groups)}")
        for g in groups:
            print(
                f"  group_id={g.id}  group_type={g.group_type!r} -> 'individual'  name={g.name!r}"
            )

        if not args.apply:
            print("\nDry-run mode: no changes written. Re-run with --apply to commit.")
            db.rollback()
            return

        for g in groups:
            g.group_type = "individual"

        db.commit()
        print(f"\napplied_updates={len(groups)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
