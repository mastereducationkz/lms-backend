"""The salary breakdown's «Ожидают отметки посещаемости» asks only about groups still running.

Reported 2026-09-11: a teacher's breakdown listed 29 lessons "awaiting marks" from groups that
had been switched off («Gulzada - Сопровождение», «K_Aldiyar SAT 2026», old Indi groups) — the
schedule keeps generating lessons for a group after it stops. The dashboard's own unmarked
queue never asked about those (`actionable_group_clause`); the payslip now asks the same.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.schemas.models import Attendance, Event, EventGroup, Group, GroupStudent, UserInDB
from tests.test_payslip_uses_the_mirrored_pay_kind import _salary, db, world  # noqa: F401 - fixtures


def _student(db, *, active=True):
    u = UserInDB(email=f"s-{datetime.utcnow().timestamp()}-{id(object())}@test.local", name="S",
                 role="student", hashed_password="x", is_active=active)
    db.add(u)
    db.flush()
    return u


def _group(db, teacher, name, *, students=1, active_students=True, **flags):
    g = Group(name=name, teacher_id=teacher.id, group_type="group",
              **{"is_active": True, "is_over": False, "is_special": False, **flags})
    db.add(g)
    db.flush()
    for _ in range(students):
        db.add(GroupStudent(group_id=g.id, student_id=_student(db, active=active_students).id,
                            created_at=datetime.utcnow() - timedelta(days=90)))
    db.flush()
    return g


def _lesson(db, teacher, g, *, marked_by=None, days_ago=2):
    when = datetime.utcnow() - timedelta(days=days_ago)
    ev = Event(title=f"{g.name}: Lesson 1", event_type="class", start_datetime=when,
               end_datetime=when + timedelta(hours=1), teacher_id=teacher.id,
               created_by=teacher.id, is_active=True)
    db.add(ev)
    db.flush()
    db.add(EventGroup(event_id=ev.id, group_id=g.id))
    if marked_by is not None:
        db.add(Attendance(event_id=ev.id, user_id=marked_by.id, status="present"))
    db.flush()
    return ev


def test_only_running_groups_are_awaiting_marks(world):
    db, teacher = world["db"], world["teacher"]
    live = _group(db, teacher, "August 19 SAT - Gulzada")
    stopped = {
        "switched off": _group(db, teacher, "Gulzada - Сопровождение", is_active=False),
        "finished": _group(db, teacher, "June 20 SAT - Gulzada", is_over=True),
        "empty": _group(db, teacher, "Indi Abzal SAT 2026 - Gulzada", students=0),
        "everyone left": _group(db, teacher, "K_Aldiyar SAT 2026 - Gulzada", active_students=False),
        "special": _group(db, teacher, "Special programme - Gulzada", is_special=True),
    }
    _lesson(db, teacher, live)
    for g in stopped.values():
        _lesson(db, teacher, g)

    body = _salary(db, teacher)

    assert [g["group_id"] for g in body["pending_groups"]] == [live.id]
    assert body["pending_lessons"] == 1
    for g in stopped.values():
        assert g.name not in body["message_text"], g.name


def test_a_marked_lesson_of_a_stopped_group_is_still_paid(world):
    db, teacher = world["db"], world["teacher"]
    stopped = _group(db, teacher, "Gulzada - Сопровождение", is_active=False)
    someone = _student(db)
    _lesson(db, teacher, stopped, marked_by=someone)

    body = _salary(db, teacher)

    assert [g["group_id"] for g in body["groups"]] == [stopped.id], "the work was registered: it is paid"
    assert body["pending_groups"] == []
