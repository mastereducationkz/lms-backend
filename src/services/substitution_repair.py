"""Find and repair lessons whose teacher disagrees with an approved substitution.

Schedule regeneration used to overwrite ``events.teacher_id`` with the group's regular
teacher, silently reverting approved substitutions (see
:mod:`src.services.lesson_teacher`). The code path is fixed; the rows it already corrupted
are not. This is the one-off — but safe-to-rerun — reconciliation for them.

Design rules, all of which exist because this touches who gets paid:

* **Dry run is the default.** ``apply=False`` opens no write transaction at all.
* **Only unambiguous mismatches are repaired.** Two approved requests naming different
  teachers is a question for a human, not a coin flip. Same for an approval that never named
  anybody, or that points at a teacher who no longer exists.
* **Pending and rejected requests are never touched.** They are proposals and refusals.
* **Every repair is audited** with before/after teacher, the requester, and this command as
  actor — so a payroll query later can be traced to a decision.
* **Idempotent.** A second run finds nothing to do, because it compares against the same
  authority the first run wrote.
* **Bounded transactions.** Repairs commit in batches, so a failure halfway leaves a
  consistent prefix rather than rolling back a thousand correct fixes.

Already-marked lessons are repaired but flagged loudly: attendance was taken by, and salary
may already have been previewed for, the wrong teacher. The row is reported so somebody can
check the money rather than discovering it on a payslip.

Usage::

    python -m src.services.substitution_repair                 # dry run, prints a summary
    python -m src.services.substitution_repair --apply         # repair unambiguous mismatches
    python -m src.services.substitution_repair --event-id 1234 # scope to one lesson
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: How many repairs share one transaction.
BATCH_SIZE = 50


@dataclass
class RepairFinding:
    """One approved substitution and what the schedule actually says about it."""

    request_id: int
    event_id: Optional[int]
    group_id: Optional[int]
    approved_teacher_id: Optional[int]
    current_teacher_id: Optional[int]
    group_teacher_id: Optional[int]
    lesson_title: Optional[str]
    start_datetime: Optional[str]
    kind: str
    attendance_marked: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "event_id": self.event_id,
            "group_id": self.group_id,
            "approved_teacher_id": self.approved_teacher_id,
            "current_teacher_id": self.current_teacher_id,
            "group_teacher_id": self.group_teacher_id,
            "lesson_title": self.lesson_title,
            "start_datetime": self.start_datetime,
            "kind": self.kind,
            "attendance_marked": self.attendance_marked,
            "detail": self.detail,
        }


@dataclass
class RepairReport:
    correct: list[RepairFinding] = field(default_factory=list)
    mismatched: list[RepairFinding] = field(default_factory=list)
    missing_event: list[RepairFinding] = field(default_factory=list)
    conflicting: list[RepairFinding] = field(default_factory=list)
    missing_teacher: list[RepairFinding] = field(default_factory=list)
    no_substitute_named: list[RepairFinding] = field(default_factory=list)
    repaired: list[RepairFinding] = field(default_factory=list)

    @property
    def marked_affected(self) -> list[RepairFinding]:
        """Mismatches on lessons whose register was already taken — check the money."""
        return [f for f in self.mismatched if f.attendance_marked]

    def summary(self) -> dict:
        return {
            "approved_requests_scanned": (
                len(self.correct)
                + len(self.mismatched)
                + len(self.missing_event)
                + len(self.conflicting)
                + len(self.missing_teacher)
                + len(self.no_substitute_named)
            ),
            "already_correct": len(self.correct),
            "mismatched": len(self.mismatched),
            "mismatched_with_attendance_marked": len(self.marked_affected),
            "missing_event": len(self.missing_event),
            "conflicting_requests": len(self.conflicting),
            "missing_teacher": len(self.missing_teacher),
            "approved_without_substitute": len(self.no_substitute_named),
            "repaired": len(self.repaired),
        }

    def as_dict(self) -> dict:
        return {
            "summary": self.summary(),
            "mismatched": [f.as_dict() for f in self.mismatched],
            "missing_event": [f.as_dict() for f in self.missing_event],
            "conflicting": [f.as_dict() for f in self.conflicting],
            "missing_teacher": [f.as_dict() for f in self.missing_teacher],
            "approved_without_substitute": [f.as_dict() for f in self.no_substitute_named],
            "repaired": [f.as_dict() for f in self.repaired],
        }


def scan(db: Session, event_id: Optional[int] = None) -> RepairReport:
    """Classify every approved substitution against the live schedule. Reads only."""
    from src.events.models import Attendance, Event, EventGroup
    from src.lesson_requests.models import LessonRequest
    from src.schemas.models import Group, UserInDB
    from src.services.attendance_status import marked_statuses_for_sql
    from src.services.lesson_teacher import (
        APPROVED_STATUS,
        SUBSTITUTION_REQUEST_TYPE,
        approved_substitute_id,
        conflicting_substitution_event_ids,
    )
    from sqlalchemy import func

    report = RepairReport()

    q = db.query(LessonRequest).filter(
        LessonRequest.request_type == SUBSTITUTION_REQUEST_TYPE,
        LessonRequest.status == APPROVED_STATUS,
    )
    if event_id is not None:
        q = q.filter(LessonRequest.event_id == event_id)
    requests = q.order_by(LessonRequest.id.asc()).all()
    if not requests:
        return report

    event_ids = sorted({int(lr.event_id) for lr in requests if lr.event_id is not None})
    conflicts = conflicting_substitution_event_ids(db, event_ids) if event_ids else {}

    events = {
        e.id: e for e in db.query(Event).filter(Event.id.in_(event_ids)).all()
    } if event_ids else {}

    group_teacher_by_event: dict[int, Optional[int]] = {}
    if event_ids:
        for ev_id, g_teacher in (
            db.query(EventGroup.event_id, Group.teacher_id)
            .join(Group, Group.id == EventGroup.group_id)
            .filter(EventGroup.event_id.in_(event_ids))
            .all()
        ):
            group_teacher_by_event.setdefault(int(ev_id), g_teacher)

    marked_event_ids: set[int] = set()
    if event_ids:
        marked_event_ids = {
            int(row[0])
            for row in db.query(Attendance.event_id)
            .filter(
                Attendance.event_id.in_(event_ids),
                func.lower(func.coalesce(Attendance.status, "")).in_(marked_statuses_for_sql()),
            )
            .distinct()
            .all()
        }

    known_teacher_ids = set()
    candidate_teacher_ids = sorted(
        {int(t) for t in (approved_substitute_id(lr) for lr in requests) if t is not None}
    )
    if candidate_teacher_ids:
        known_teacher_ids = {
            int(row[0])
            for row in db.query(UserInDB.id).filter(UserInDB.id.in_(candidate_teacher_ids)).all()
        }

    for lr in requests:
        approved_teacher_id = approved_substitute_id(lr)
        event = events.get(int(lr.event_id)) if lr.event_id is not None else None
        finding = RepairFinding(
            request_id=lr.id,
            event_id=lr.event_id,
            group_id=lr.group_id,
            approved_teacher_id=approved_teacher_id,
            current_teacher_id=event.teacher_id if event else None,
            group_teacher_id=group_teacher_by_event.get(int(lr.event_id)) if lr.event_id else None,
            lesson_title=event.title if event else None,
            start_datetime=(
                event.start_datetime.isoformat()
                if event and event.start_datetime
                else (lr.original_datetime.isoformat() if lr.original_datetime else None)
            ),
            attendance_marked=bool(lr.event_id and int(lr.event_id) in marked_event_ids),
            kind="",
        )

        if approved_teacher_id is None:
            finding.kind = "approved_without_substitute"
            finding.detail = "Заявка одобрена, но замещающий педагог не указан"
            report.no_substitute_named.append(finding)
            continue
        if lr.event_id is None or event is None:
            finding.kind = "missing_event"
            finding.detail = "Одобренная замена не привязана к существующему уроку"
            report.missing_event.append(finding)
            continue
        if int(lr.event_id) in conflicts:
            finding.kind = "conflicting"
            finding.detail = (
                "Несколько одобренных заявок называют разных педагогов: "
                + ", ".join(str(t) for t in conflicts[int(lr.event_id)])
            )
            report.conflicting.append(finding)
            continue
        if int(approved_teacher_id) not in known_teacher_ids:
            finding.kind = "missing_teacher"
            finding.detail = "Назначенный педагог не найден"
            report.missing_teacher.append(finding)
            continue
        if event.teacher_id == approved_teacher_id:
            finding.kind = "correct"
            report.correct.append(finding)
            continue

        finding.kind = "mismatch"
        finding.detail = (
            f"Урок закреплён за педагогом {event.teacher_id}, "
            f"одобрена замена на {approved_teacher_id}"
        )
        report.mismatched.append(finding)

    return report


def repair(db: Session, event_id: Optional[int] = None, actor_id: Optional[int] = None) -> RepairReport:
    """Scan, then fix the unambiguous mismatches. Writes in bounded batches.

    Idempotent by construction: the fix makes ``events.teacher_id`` equal the approved
    substitute, which is exactly the condition :func:`scan` classifies as ``correct``.
    """
    from src.events.models import Event
    from src.services.lesson_teacher import record_lesson_teacher_audit

    report = scan(db, event_id=event_id)
    if not report.mismatched:
        return report

    pending = 0
    for finding in report.mismatched:
        event = db.query(Event).filter(Event.id == finding.event_id).first()
        if not event:  # pragma: no cover - raced with a delete
            continue
        # Re-check under the write: another process may have fixed it since the scan.
        if event.teacher_id == finding.approved_teacher_id:
            continue

        before = event.teacher_id
        event.teacher_id = finding.approved_teacher_id
        record_lesson_teacher_audit(
            db,
            event_id=event.id,
            before_teacher_id=before,
            after_teacher_id=finding.approved_teacher_id,
            action="lesson.substitution.repaired",
            actor_id=actor_id,
            lesson_request_id=finding.request_id,
            reason="substitution_repair: schedule regeneration had reverted an approved substitution",
        )
        report.repaired.append(finding)
        pending += 1
        if pending >= BATCH_SIZE:
            db.commit()
            pending = 0

    if pending:
        db.commit()
    return report


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write repairs (default: dry run)")
    parser.add_argument("--event-id", type=int, default=None, help="Scope to one lesson")
    parser.add_argument("--actor-id", type=int, default=None, help="User id to record as actor")
    parser.add_argument("--json", action="store_true", help="Emit the full report as JSON")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from src.config import SessionLocal

    db = SessionLocal()
    try:
        report = (
            repair(db, event_id=args.event_id, actor_id=args.actor_id)
            if args.apply
            else scan(db, event_id=args.event_id)
        )
    finally:
        db.close()

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        mode = "APPLY" if args.apply else "DRY RUN (no changes written)"
        print(f"substitution repair — {mode}")
        for key, value in report.summary().items():
            print(f"  {key}: {value}")
        for label, rows in (
            ("MISMATCHED", report.mismatched if not args.apply else report.repaired),
            ("CONFLICTING (left untouched)", report.conflicting),
            ("MISSING EVENT (left untouched)", report.missing_event),
            ("MISSING TEACHER (left untouched)", report.missing_teacher),
            ("APPROVED WITHOUT SUBSTITUTE (left untouched)", report.no_substitute_named),
        ):
            if not rows:
                continue
            print(f"\n{label}:")
            for f in rows:
                marked = " [ATTENDANCE ALREADY MARKED]" if f.attendance_marked else ""
                print(
                    f"  request={f.request_id} event={f.event_id} "
                    f"current={f.current_teacher_id} approved={f.approved_teacher_id} "
                    f"{f.start_datetime or ''} {f.lesson_title or ''}{marked}"
                )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
