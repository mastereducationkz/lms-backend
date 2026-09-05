"""Pilot operations for SAT Checkpoints: readiness report, activation, per-group enablement.

    python -m scripts.checkpoint_pilot report   --course-id 1 --group-ids 243,236,295
    python -m scripts.checkpoint_pilot activate --course-id 1 [--dry-run]
    python -m scripts.checkpoint_pilot enable   --group-id 243 --start 5 [--dry-run]
    python -m scripts.checkpoint_pilot disable  --group-id 243

`report` shows, per group and student, the highest block whose required units are all complete,
and suggests a start number: the block most of the group is working on (median highest completed
block + 1), so that enabling the group opens at most the checkpoint they just finished rather than
every checkpoint of the blocks they did weeks ago. It also lists what would open immediately at
that start number.

`enable` sets the start number, switches the group on and runs the same sync the console runs, so
every student whose blocks are already complete gets their checkpoints (deadlines a day apart in
checkpoint order). `activate` switches on every definition whose quiz holds total_questions
questions.
"""
import argparse
import json
import statistics
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from src.checkpoints import service
from src.checkpoints.completion import completed_lesson_ids
from src.checkpoints.models import CheckpointDefinition
from src.courses.models import Group, GroupStudent, Step
from src.schemas.models import UserInDB


def _definitions(db: Session, course_id: int) -> List[CheckpointDefinition]:
    return db.query(CheckpointDefinition).filter_by(course_id=course_id).order_by(CheckpointDefinition.number).all()


def group_report(db: Session, course_id: int, group_id: int) -> Dict[str, Any]:
    group = db.get(Group, group_id)
    if group is None:
        raise SystemExit(f"group {group_id} not found")
    definitions = _definitions(db, course_id)
    required_all = list(dict.fromkeys(u.lesson_id for d in definitions for u in d.required_units))
    students = (db.query(UserInDB).join(GroupStudent, GroupStudent.student_id == UserInDB.id)
                .filter(GroupStudent.group_id == group.id).order_by(UserInDB.name).all())
    rows = []
    for s in students:
        done = completed_lesson_ids(db, s.id, required_all)
        complete = [d.number for d in definitions if d.required_units and {u.lesson_id for u in d.required_units} <= done]
        highest = 0
        for n in sorted(complete):
            if n == highest + 1:
                highest = n
            else:
                break
        rows.append({"student_id": s.id, "name": s.name, "highest_contiguous_block": highest,
                     "complete_blocks": complete})
    highs = [r["highest_contiguous_block"] for r in rows]
    suggested = (int(statistics.median(highs)) + 1) if highs else 1
    suggested = max(1, min(suggested, max((d.number for d in definitions), default=1)))
    for r in rows:
        r["would_open_now"] = [n for n in r["complete_blocks"] if n >= suggested]
    return {"group_id": group.id, "name": group.name, "enabled": bool(group.checkpoints_enabled),
            "start_number": group.checkpoints_start_number, "students": rows, "suggested_start": suggested,
            "opens_immediately": sum(len(r["would_open_now"]) for r in rows)}


def activate(db: Session, course_id: int, *, dry_run: bool = False) -> List[Dict[str, Any]]:
    out = []
    for d in _definitions(db, course_id):
        step = db.query(Step).filter(Step.lesson_id == d.quiz_lesson_id, Step.content_type == "quiz").first()
        try:
            count = len(json.loads(step.content_text).get("questions") or []) if step and step.content_text else 0
        except ValueError:
            count = 0
        ok = count == d.total_questions
        if ok and not dry_run:
            d.is_active = True
        out.append({"number": d.number, "questions": count, "expected": d.total_questions,
                    "active": bool(d.is_active) if not dry_run else ok})
    if not dry_run:
        db.commit()
        service._invalidate_lesson_caches()
    return out


def enable(db: Session, group_id: int, start: int, *, dry_run: bool = False) -> Dict[str, Any]:
    group = db.get(Group, group_id)
    if group is None:
        raise SystemExit(f"group {group_id} not found")
    if dry_run:
        return {"group_id": group.id, "name": group.name, "start_number": start, "opened": None, "dry_run": True}
    group.checkpoints_start_number = start
    group.checkpoints_enabled = True
    db.commit()
    opened = service.sync_group(db, group, commit=True)
    service._invalidate_lesson_caches()
    return {"group_id": group.id, "name": group.name, "start_number": start, "opened": opened, "dry_run": False}


def disable(db: Session, group_id: int) -> Dict[str, Any]:
    group = db.get(Group, group_id)
    if group is None:
        raise SystemExit(f"group {group_id} not found")
    group.checkpoints_enabled = False
    db.commit()
    service._invalidate_lesson_caches()
    return {"group_id": group.id, "name": group.name, "enabled": False}


def main() -> None:  # pragma: no cover - operator CLI
    from src.config import SessionLocal
    ap = argparse.ArgumentParser(description="SAT Checkpoints pilot operations")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report"); r.add_argument("--course-id", type=int, required=True); r.add_argument("--group-ids", required=True)
    a = sub.add_parser("activate"); a.add_argument("--course-id", type=int, required=True); a.add_argument("--dry-run", action="store_true")
    e = sub.add_parser("enable"); e.add_argument("--group-id", type=int, required=True); e.add_argument("--start", type=int, required=True); e.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("disable"); d.add_argument("--group-id", type=int, required=True)
    args = ap.parse_args()
    db = SessionLocal()
    try:
        if args.cmd == "report":
            for gid in [int(x) for x in args.group_ids.split(",") if x.strip()]:
                rep = group_report(db, args.course_id, gid)
                print(f"\n=== {rep['group_id']} {rep['name']} — enabled={rep['enabled']} start={rep['start_number']} "
                      f"— suggested start {rep['suggested_start']} ({rep['opens_immediately']} checkpoint(s) would open now)")
                for s in rep["students"]:
                    print(f"  {s['name']:40s} blocks done {s['complete_blocks']}  contiguous {s['highest_contiguous_block']}  "
                          f"opens now {s['would_open_now']}")
        elif args.cmd == "activate":
            for row in activate(db, args.course_id, dry_run=args.dry_run):
                print(f"Checkpoint {row['number']}: {row['questions']}/{row['expected']} questions -> "
                      f"{'active' if row['active'] else 'NOT activated'}{' [dry-run]' if args.dry_run else ''}")
        elif args.cmd == "enable":
            print(enable(db, args.group_id, args.start, dry_run=args.dry_run))
        elif args.cmd == "disable":
            print(disable(db, args.group_id))
    finally:
        db.close()


if __name__ == "__main__":
    main()
