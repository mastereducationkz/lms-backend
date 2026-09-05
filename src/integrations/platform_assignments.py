"""Auto-created platform-test assignments (Platform Integration Pack §6.3, E1).

One regular ``Assignment`` (type ``platform_test``) per (weekly set, IELTS-track group), linked
through ``platform_test_assignments``. Being a normal assignment with ``group_id`` + ``due_date``
it shows in every existing list/feed and in the calendar's synthesized "Deadline:" event.
Weekly-set events and the nightly job call the same idempotent ``sync_weekly_set``:

* published/updated → create for every target group (only while the set is current, unless
  ``include_past``), recompute title/due/modules on existing rows, re-activate deactivated rows;
* unpublished, or a group that stopped qualifying (opted out, ended) → deactivate, delete nothing.

Gated by ``PLATFORM_ASSIGNMENTS_ENABLED`` (off by default: no rows, ever).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.integrations.models import PlatformTestAssignment, PlatformWeeklySet

_TRUTHY = {"1", "true", "yes", "on"}
ASSIGNMENT_TYPE = "platform_test"
PLATFORM_LABEL = {"ielts": "IELTS", "sat": "SAT", "nuet": "NUET"}


def enabled() -> bool:
    return os.getenv("PLATFORM_ASSIGNMENTS_ENABLED", "").strip().lower() in _TRUTHY


def homework_enabled() -> bool:
    """Lead decision 2026-09-05: weekly tests are calendar events, not homework. Homework rows are
    only created while PLATFORM_TEST_HOMEWORK is on (off by default); with it off every existing
    platform_test assignment is deactivated by the next sync (rows kept, reversible)."""
    return os.getenv("PLATFORM_TEST_HOMEWORK", "").strip().lower() in _TRUTHY


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_utc(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def track_of(ws: PlatformWeeklySet) -> str:
    """The LMS program a set is for: the emitter's track (sat|nuet|ielts), else the platform."""
    return str(ws.track or ws.platform or "").lower()


def module_path(module: str, test_id, weekly_set_id, platform: str = "ielts") -> Optional[str]:
    """LMS-side default path of a part when the emitter did not send one. Only IELTS has such
    rules (Writing has no auto-start URL — the timed session must be started by the student's
    own click — so it goes to the set page); other platforms send ``path`` in the event."""
    if (platform or "ielts").lower() != "ielts":
        return None
    module = (module or "").lower()
    if module in ("listening", "reading"):
        return f"/exam/test/{test_id}"
    if module == "speaking":
        return f"/speaking-ai/setup/{test_id}"
    return f"/weekly-sets/{weekly_set_id}"


def default_set_path(ws: PlatformWeeklySet) -> Optional[str]:
    return f"/weekly-sets/{ws.weekly_set_id}" if (ws.platform or "").lower() == "ielts" else None


def target_groups(db: Session, program: str = "ielts") -> list:
    """Active, non-special groups of the program (sat | nuet | ielts) that have not opted out."""
    from src.courses.models import Group

    return (
        db.query(Group)
        .filter(
            func.lower(Group.program_type) == (program or "").lower(),
            or_(Group.is_active.is_(True), Group.is_active.is_(None)),
            or_(Group.is_special.is_(False), Group.is_special.is_(None)),
            or_(Group.platform_tests_opt_out.is_(False), Group.platform_tests_opt_out.is_(None)),
        )
        .order_by(Group.id.asc())
        .all()
    )


def build_title(ws: PlatformWeeklySet) -> str:
    track = track_of(ws)
    label = PLATFORM_LABEL.get(track, track.upper())
    return f"{label} Weekly Test · {ws.title or ws.weekly_set_id}"


def build_content(ws: PlatformWeeklySet) -> dict:
    modules = []
    for entry in ws.modules or []:
        module = str(entry.get("module") or "").lower()
        test_id = entry.get("test_id")
        modules.append({
            "module": module,
            "test_id": test_id,
            "test_title": entry.get("test_title"),
            "path": entry.get("path") or module_path(module, test_id, ws.weekly_set_id, ws.platform),
        })
    return {
        "platform": ws.platform,
        "track": track_of(ws),
        "weekly_set_id": ws.weekly_set_id,
        "title": ws.title,
        "date_from": _iso_utc(ws.date_from),
        "date_to": _iso_utc(ws.date_to),
        "set_path": getattr(ws, "set_path", None) or default_set_path(ws),
        "modules": modules,
    }


def build_description(ws: PlatformWeeklySet) -> str:
    track = track_of(ws)
    label = PLATFORM_LABEL.get(track, track.upper())
    parts = ", ".join(m["module"].capitalize() for m in build_content(ws)["modules"]) or "all parts"
    return (f"Weekly test on the {label} platform: {parts}. "
            "Open each part from this page; your checkmarks update automatically.")


def _apply(assignment, ws: PlatformWeeklySet) -> None:
    assignment.title = build_title(ws)
    assignment.description = build_description(ws)
    assignment.content = json.dumps(build_content(ws), ensure_ascii=False)
    assignment.due_date = ws.date_to
    assignment.is_active = True
    assignment.is_hidden = False


def _links_for_set(db: Session, ws: PlatformWeeklySet) -> dict:
    rows = (
        db.query(PlatformTestAssignment)
        .filter(PlatformTestAssignment.platform == ws.platform,
                PlatformTestAssignment.weekly_set_id == ws.weekly_set_id)
        .all()
    )
    return {r.group_id: r for r in rows}


def is_past(ws: PlatformWeeklySet, now: Optional[datetime] = None) -> bool:
    now = now or _utcnow()
    return ws.date_to is not None and ws.date_to < now


def has_window(ws: PlatformWeeklySet) -> bool:
    """A set with no closing time can never be "this week's" set: NUET sets are curriculum weeks
    that the platform opens per group, so the LMS cannot tell which one is current — such sets
    never carry an assignment (existing ones are deactivated, never deleted)."""
    return ws.date_to is not None


def sync_weekly_set(db: Session, ws: PlatformWeeklySet, *, now: Optional[datetime] = None,
                    include_past: bool = False) -> dict:
    """Create/update the assignment of every target group; deactivate the rest. Idempotent."""
    from src.assignments.models import Assignment

    out = {"created": 0, "updated": 0, "deactivated": 0}
    if not enabled():
        return out
    now = now or _utcnow()
    assignable = bool(ws.is_active) and has_window(ws) and homework_enabled()
    targets = {g.id: g for g in target_groups(db, track_of(ws))} if assignable else {}
    may_create = assignable and (include_past or not is_past(ws, now))
    links = _links_for_set(db, ws)

    for group_id, link in links.items():
        assignment = db.query(Assignment).filter(Assignment.id == link.assignment_id).first()
        if assignment is None:
            continue
        if group_id in targets:
            _apply(assignment, ws)
            out["updated"] += 1
        elif assignment.is_active:
            assignment.is_active = False  # unpublished / no window / group left the target set; nothing deleted
            out["deactivated"] += 1

    if may_create:
        for group_id, group in targets.items():
            if group_id in links:
                continue
            assignment = Assignment(assignment_type=ASSIGNMENT_TYPE, group_id=group.id, title="", content="{}")
            _apply(assignment, ws)
            db.add(assignment)
            db.flush()
            db.add(PlatformTestAssignment(assignment_id=assignment.id, platform=ws.platform,
                                          weekly_set_id=ws.weekly_set_id, group_id=group.id))
            out["created"] += 1
    db.commit()
    return out


def sync_all_active(db: Session, platform: str = "ielts", *, now: Optional[datetime] = None,
                    include_past: bool = False) -> dict:
    """Run ``sync_weekly_set`` for every active set of the platform (nightly job, enable-time CLI)."""
    sets = (
        db.query(PlatformWeeklySet)
        .filter(PlatformWeeklySet.platform == platform, PlatformWeeklySet.is_active.is_(True))
        .order_by(PlatformWeeklySet.weekly_set_id.asc())
        .all()
    )
    out = {"sets": 0, "created": 0, "updated": 0, "deactivated": 0}
    for ws in sets:
        result = sync_weekly_set(db, ws, now=now, include_past=include_past)
        out["sets"] += 1
        for key in ("created", "updated", "deactivated"):
            out[key] += result[key]
    return out


def set_group_opt_out(db: Session, group, opt_out: bool, *, now: Optional[datetime] = None) -> dict:
    """Flip a group's opt-out and re-sync: opting out detaches the group from the current
    weekly-test calendar events (and deactivates any homework rows), opting back in reattaches."""
    from src.integrations import platform_calendar

    group.platform_tests_opt_out = bool(opt_out)
    db.commit()
    program = (group.program_type or "ielts").lower()
    platform = "sat" if program in ("sat", "nuet") else program
    out = sync_all_active(db, platform, now=now)
    out["calendar"] = platform_calendar.sync_all_active(db, platform, now=now)
    return out


def main() -> None:  # pragma: no cover - operator CLI
    """``python -m src.integrations.platform_assignments [--include-past] [--dry-run]``"""
    import argparse

    from src.config import SessionLocal

    parser = argparse.ArgumentParser(description="Sync platform_test assignments from stored weekly sets")
    parser.add_argument("--include-past", action="store_true", help="also create for sets whose date_to has passed")
    parser.add_argument("--platform", default="ielts")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        print(sync_all_active(db, args.platform, include_past=args.include_past))
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    main()
