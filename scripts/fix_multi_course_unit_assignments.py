"""
Repair multi_task assignments that contain more than one `course_unit` task
(the "Null course" bug).

Fix, per assignment:
  1. DROP empty course_unit tasks (no lesson_ids) — these are the uncompletable
     null-course leftovers a teacher created by double-clicking "Course Units".
  2. MERGE the remaining (configured) course_unit tasks that share one course_id
     into a single course_unit task: lesson_ids = de-duplicated union, points =
     sum, is_optional = True only if all merged were optional. Missing course_id
     is resolved from the first lesson (lesson -> module -> course_id), mirroring
     the backend's normalize_multi_task_content().
Non-course_unit tasks are kept, in place. The merged/kept course_unit task takes
the position of the first course_unit task.

If, after dropping empties, the configured course_unit tasks still span MULTIPLE
distinct course_ids, the assignment is SKIPPED (reported for manual review).

Usage (from lms/backend):
    # Dry run — prints the plan, changes nothing:
    PYTHONPATH=. ./venv/bin/python scripts/fix_multi_course_unit_assignments.py

    # Apply — writes a JSON backup of originals, then updates the DB:
    PYTHONPATH=. ./venv/bin/python scripts/fix_multi_course_unit_assignments.py --apply
"""
import json
import os
import sys

from src.config import SessionLocal
from src.schemas.models import Assignment, Lesson, Module

# Backup destination must be writable. On prod /app/scripts is a read-only mount,
# so override with BACKUP_PATH=/app/logs/... (a writable mounted dir).
BACKUP_PATH = os.getenv("BACKUP_PATH", "scripts/multi_course_unit_backup.json")


def load_content(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return raw if isinstance(raw, dict) else None


def dump_same_type(original_raw, new_content):
    if isinstance(original_raw, str):
        return json.dumps(new_content, ensure_ascii=False)
    return new_content


def resolve_course_id(db, task_content):
    """Explicit course_id, else derived from the first lesson (lesson->module->course)."""
    cid = task_content.get("course_id")
    if cid:
        return cid
    lesson_ids = task_content.get("lesson_ids") or []
    if not lesson_ids:
        return None
    lesson = db.query(Lesson).filter(Lesson.id == lesson_ids[0]).first()
    if not lesson:
        return None
    module = db.query(Module).filter(Module.id == lesson.module_id).first()
    return module.course_id if module else None


def plan_fix(db, tasks):
    """Return (new_tasks, note) or (None, skip_reason)."""
    cu_tasks = [t for t in tasks if isinstance(t, dict) and t.get("task_type") == "course_unit"]
    if len(cu_tasks) <= 1:
        return None, "no-op"

    empty = [t for t in cu_tasks if not (t.get("content", {}) or {}).get("lesson_ids")]
    configured = [t for t in cu_tasks if (t.get("content", {}) or {}).get("lesson_ids")]

    if not configured:
        return None, "SKIP: all course_unit tasks are empty (no lessons)"

    eff_ids = {resolve_course_id(db, t.get("content", {}) or {}) for t in configured}
    eff_ids.discard(None)
    if len(eff_ids) > 1:
        return None, f"SKIP: configured course_unit tasks span course_ids {sorted(map(str, eff_ids))}"

    effective_course_id = next(iter(eff_ids)) if eff_ids else None

    # Union lesson_ids (first-seen order), sum points, optional only if all optional.
    merged_lessons, seen = [], set()
    total_points = 0
    all_optional = True
    for t in configured:
        c = t.get("content", {}) or {}
        for lid in (c.get("lesson_ids") or []):
            if lid not in seen:
                seen.add(lid)
                merged_lessons.append(lid)
        total_points += t.get("points", 0) or 0
        if not t.get("is_optional", False):
            all_optional = False

    first_cu = configured[0]
    merged_task = dict(first_cu)
    merged_task["content"] = dict(first_cu.get("content", {}) or {})
    merged_task["content"]["lesson_ids"] = merged_lessons
    if effective_course_id is not None:
        merged_task["content"]["course_id"] = effective_course_id
    merged_task["points"] = total_points
    merged_task["is_optional"] = all_optional

    # Rebuild: replace the first course_unit slot with merged_task; drop all other
    # course_unit tasks (extra configured + empties); keep everything else in place.
    new_tasks, inserted = [], False
    for t in tasks:
        if isinstance(t, dict) and t.get("task_type") == "course_unit":
            if not inserted:
                new_tasks.append(merged_task)
                inserted = True
        else:
            new_tasks.append(t)

    for i, t in enumerate(new_tasks):
        if isinstance(t, dict):
            t["order_index"] = i

    note = (
        f"drop {len(empty)} empty + merge {len(configured)} -> 1 "
        f"(course_id={effective_course_id}, lessons={merged_lessons}, pts={total_points})"
    )
    return new_tasks, note


def main():
    apply = "--apply" in sys.argv
    db = SessionLocal()
    try:
        assignments = db.query(Assignment).filter(
            Assignment.assignment_type == "multi_task"
        ).all()

        backups, fixed, skipped = [], [], []

        for a in assignments:
            content = load_content(a.content)
            if not content or not isinstance(content.get("tasks"), list):
                continue
            tasks = content["tasks"]
            cu_count = sum(
                1 for t in tasks if isinstance(t, dict) and t.get("task_type") == "course_unit"
            )
            if cu_count <= 1:
                continue

            new_tasks, note = plan_fix(db, tasks)
            if new_tasks is None:
                skipped.append((a.id, a.title, note))
                continue

            print(f"[{a.id}] {(a.title or '').strip()[:50]}: {note}")

            if apply:
                backups.append({"id": a.id, "title": a.title, "original_content": a.content})
                new_content = dict(content)
                new_content["tasks"] = new_tasks
                req = sum((t.get("points", 0) or 0) for t in new_tasks if not t.get("is_optional"))
                bonus = sum((t.get("points", 0) or 0) for t in new_tasks if t.get("is_optional"))
                new_content["required_points"] = req
                new_content["bonus_points"] = bonus
                new_content["total_points"] = req + bonus
                a.content = dump_same_type(a.content, new_content)
            fixed.append(a.id)

        print()
        print(f"Would fix: {len(fixed)}   Skipped (manual review): {len(skipped)}")
        for sid, stitle, sreason in skipped:
            print(f"  SKIP [{sid}] {(stitle or '').strip()[:40]}: {sreason}")

        if apply and fixed:
            with open(BACKUP_PATH, "w", encoding="utf-8") as f:
                json.dump(backups, f, ensure_ascii=False, indent=2, default=str)
            db.commit()
            print(f"\nAPPLIED to {len(fixed)} assignments. Backup written to {BACKUP_PATH}")
        elif not apply:
            print("\nDry run only. Re-run with --apply to write changes.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
