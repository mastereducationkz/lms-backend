"""
Read-only audit: find multi_task assignments that contain more than one
`course_unit` task. Those trigger the "Null course" bug (one course-completion
task becomes impossible to complete).

Usage (from lms/backend):
    ./venv/bin/python scripts/find_multi_course_unit_assignments.py

Prints a table of offenders. Makes NO changes.
"""
import json

from src.config import SessionLocal
from src.schemas.models import Assignment


def course_unit_count(content):
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return 0
    if not isinstance(content, dict):
        return 0
    tasks = content.get("tasks")
    if not isinstance(tasks, list):
        return 0
    return sum(
        1 for t in tasks
        if isinstance(t, dict) and t.get("task_type") == "course_unit"
    )


def main():
    db = SessionLocal()
    try:
        assignments = db.query(Assignment).filter(
            Assignment.assignment_type == "multi_task"
        ).all()

        offenders = []
        for a in assignments:
            n = course_unit_count(a.content)
            if n > 1:
                offenders.append((a, n))

        print(f"Scanned {len(assignments)} multi_task assignments.")
        print(f"Found {len(offenders)} with >1 course_unit task.\n")

        if not offenders:
            print("Nothing to fix. ✅")
            return

        print(f"{'ID':>6}  {'#CU':>3}  {'active':>6}  {'group':>6}  {'lesson':>6}  title")
        print("-" * 80)
        for a, n in sorted(offenders, key=lambda x: x[1], reverse=True):
            print(
                f"{a.id:>6}  {n:>3}  {str(a.is_active):>6}  "
                f"{str(a.group_id or ''):>6}  {str(a.lesson_id or ''):>6}  "
                f"{(a.title or '')[:50]}"
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
