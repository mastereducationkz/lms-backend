"""Per-student progress on platform-test assignments (E1 checkmarks, E2 countdown data).

Nothing here is stored: module states are read from ``platform_results`` (kept by Phase 1).
The one write is the auto ``AssignmentSubmission`` when every module the set contains is done,
so the existing "submitted" lists/feeds stay truthful; it is ``is_graded=True, score=None`` so
grading queues (which filter ``is_graded == False``) never show it.

Date semantics (IELTS): Listening/Reading/Writing are "due" at date_to and stay takeable while
the set is active; the AI Speaking part "closes" at date_to's exact minute and is takeable only
inside [date_from, date_to].
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.integrations.models import PlatformResult, PlatformTestAssignment

DONE_STATUSES = {"submitted", "expired", "completed", "scored"}
AUTO_FEEDBACK = "Completed on the platform"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def module_semantics(module: str, date_from: Optional[datetime], date_to: Optional[datetime], now: datetime) -> dict:
    if module == "speaking":
        inside = (date_from is None or now >= date_from) and (date_to is None or now <= date_to)
        return {"deadline_kind": "closes", "available": inside}
    return {"deadline_kind": "due", "available": True}


def module_states(db: Session, content: dict, user_id: Optional[int], now: Optional[datetime] = None) -> list[dict]:
    now = now or _utcnow()
    platform = content.get("platform", "ielts")
    set_id = content.get("weekly_set_id")
    d_from, d_to = _parse(content.get("date_from")), _parse(content.get("date_to"))
    latest: dict[str, PlatformResult] = {}
    if user_id is not None and set_id is not None:
        rows = (
            db.query(PlatformResult)
            .filter(PlatformResult.platform == platform, PlatformResult.user_id == user_id,
                    PlatformResult.weekly_set_id == int(set_id))
            .order_by(PlatformResult.updated_at.desc(), PlatformResult.id.desc())
            .all()
        )
        for r in rows:
            latest.setdefault(r.module, r)
    states = []
    for entry in content.get("modules") or []:
        module = str(entry.get("module") or "")
        r = latest.get(module)
        if r is None:
            state = "not_started"
        elif r.status in DONE_STATUSES:
            state = "done"
        else:
            state = "in_progress"
        finished = (r.scored_at or r.finished_at) if r is not None else None
        states.append({
            "module": module,
            "test_title": entry.get("test_title"),
            "path": entry.get("path"),
            "state": state,
            "band": r.band if r is not None else None,
            "result_url": r.result_url if r is not None else None,
            "finished_at": _iso(finished),
            **module_semantics(module, d_from, d_to, now),
        })
    return states


def summarize(states: list[dict]) -> str:
    if states and all(s["state"] == "done" for s in states):
        return "submitted"
    if any(s["state"] != "not_started" for s in states):
        return "in_progress"
    return "not_started"


def _base(assignment, content: dict, now: datetime) -> dict:
    d_to = _parse(content.get("date_to"))
    return {
        "assignment_id": assignment.id,
        "group_id": assignment.group_id,
        "title": assignment.title,
        "platform": content.get("platform"),
        "track": content.get("track") or content.get("platform"),
        "weekly_set_id": content.get("weekly_set_id"),
        "set_title": content.get("title"),
        "set_path": content.get("set_path") or (
            f"/weekly-sets/{content.get('weekly_set_id')}" if content.get("platform") == "ielts" else None
        ),
        "date_from": content.get("date_from"),
        "date_to": content.get("date_to"),
        "due_date": _iso(assignment.due_date),
        "days_left": (d_to - now).days if d_to else None,
        "is_active": bool(assignment.is_active),
    }


def assignment_summary(assignment, now: Optional[datetime] = None) -> dict:
    """The set-level facts of a platform-test assignment (no student in scope)."""
    return _base(assignment, json.loads(assignment.content or "{}"), now or _utcnow())


def student_progress(db: Session, assignment, user_id: int, now: Optional[datetime] = None) -> dict:
    now = now or _utcnow()
    content = json.loads(assignment.content or "{}")
    states = module_states(db, content, user_id, now)
    return {**_base(assignment, content, now), "status": summarize(states), "modules": states}


def group_matrix(db: Session, assignment, now: Optional[datetime] = None) -> list[dict]:
    from src.auth.models import UserInDB
    from src.courses.models import GroupStudent

    now = now or _utcnow()
    content = json.loads(assignment.content or "{}")
    students = (
        db.query(UserInDB)
        .join(GroupStudent, GroupStudent.student_id == UserInDB.id)
        .filter(GroupStudent.group_id == assignment.group_id)
        .order_by(UserInDB.id.asc())
        .all()
    )
    rows = []
    for s in students:
        states = module_states(db, content, s.id, now)
        rows.append({"user_id": s.id, "name": s.name, "email": s.email, "status": summarize(states), "modules": states})
    return rows


def weekly_tests_for_student(db: Session, user_id: int, now: Optional[datetime] = None) -> list[dict]:
    """The student's active platform tests: current ones first (nearest due first), then past ones
    (most recent first) — the dashboard shows the first item."""
    from src.assignments.models import Assignment
    from src.courses.models import GroupStudent

    now = now or _utcnow()
    group_ids = [gid for (gid,) in db.query(GroupStudent.group_id).filter(GroupStudent.student_id == user_id).all()]
    if not group_ids:
        return []
    rows = (
        db.query(Assignment)
        .join(PlatformTestAssignment, PlatformTestAssignment.assignment_id == Assignment.id)
        .filter(Assignment.group_id.in_(group_ids), Assignment.is_active.is_(True))
        .all()
    )
    items = [student_progress(db, a, user_id, now) for a in rows]
    current = [i for i in items if i["days_left"] is None or i["days_left"] >= 0]
    past = [i for i in items if i["days_left"] is not None and i["days_left"] < 0]
    current.sort(key=lambda i: (i["days_left"] is None, i["days_left"] if i["days_left"] is not None else 0))
    past.sort(key=lambda i: -i["days_left"])
    return current + past


def upsert_auto_submission(db: Session, assignment, user_id: int, states: list[dict]) -> bool:
    """Write the auto submission once every module is done; returns True when a row was written."""
    from src.assignments.models import AssignmentSubmission

    if summarize(states) != "submitted":
        return False
    existing = (
        db.query(AssignmentSubmission)
        .filter(AssignmentSubmission.assignment_id == assignment.id, AssignmentSubmission.user_id == user_id)
        .first()
    )
    if existing is not None:
        return False
    finished = [_parse(s["finished_at"]) for s in states if s.get("finished_at")]
    submitted_at = max(finished) if finished else _utcnow()
    db.add(AssignmentSubmission(
        assignment_id=assignment.id,
        user_id=user_id,
        answers=json.dumps({"modules": states}, ensure_ascii=False),
        max_score=assignment.max_score or 100,
        score=None,
        is_graded=True,
        feedback=AUTO_FEEDBACK,
        submitted_at=submitted_at,
        is_late=bool(assignment.due_date and submitted_at > assignment.due_date),
    ))
    db.commit()
    return True


def on_result_change(db: Session, platform: str, weekly_set_id, user_id: Optional[int]) -> int:
    """After a student's platform_results row changed: write the auto submission for every
    platform_test assignment of that set in the student's groups whose modules are now all done."""
    from src.assignments.models import Assignment
    from src.courses.models import GroupStudent
    from src.integrations import platform_assignments

    if user_id is None or weekly_set_id is None or not platform_assignments.enabled():
        return 0
    group_ids = [gid for (gid,) in db.query(GroupStudent.group_id).filter(GroupStudent.student_id == user_id).all()]
    if not group_ids:
        return 0
    rows = (
        db.query(Assignment)
        .join(PlatformTestAssignment, PlatformTestAssignment.assignment_id == Assignment.id)
        .filter(PlatformTestAssignment.platform == platform,
                PlatformTestAssignment.weekly_set_id == int(weekly_set_id),
                Assignment.group_id.in_(group_ids))
        .all()
    )
    written = 0
    for assignment in rows:
        states = module_states(db, json.loads(assignment.content or "{}"), user_id)
        if upsert_auto_submission(db, assignment, user_id, states):
            written += 1
    return written
