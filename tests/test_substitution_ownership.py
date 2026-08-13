"""The June 15 Lesson 32 incident, end to end.

Group "June 15 SAT - Azamat" belongs to Azamat. Lesson 32 was approved to be covered by
Nuray. Three separate defects then conspired:

1. schedule regeneration overwrote ``events.teacher_id`` with the group's teacher, handing
   the lesson back to Azamat;
2. the teacher dashboard derived "your unmarked lessons" from *groups you own*, so the
   lesson sat in Azamat's queue and never appeared in Nuray's;
3. the salary breakdown paid every completed lesson whether or not anybody had taken the
   register, so once Azamat marked a lesson he had not taught, he was paid for it.

Each is asserted here against the real behaviour, not against a helper in isolation — a
test that only exercises ``approved_substitution_map`` would have passed throughout the
incident.

The invariants under test:

* ``groups.teacher_id`` is ownership and never moves.
* ``events.teacher_id`` is who taught it, and an approved substitution pins it.
* Attendance duty and payment both follow ``events.teacher_id``.
* Covering somebody's lesson does not enlarge the substitute's group count.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

# Everything through the re-export shim: importing `src.events.models` directly ahead of it
# trips the circular import between the domain models and the schema aggregate.
from src.schemas.models import (
    Attendance,
    Event,
    EventGroup,
    Group,
    GroupStudent,
    LessonRequest,
    UserInDB,
)


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine

    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available (requires Postgres); skipping")

    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        trans.rollback()


def _user(db, name: str, role: str = "teacher") -> UserInDB:
    u = UserInDB(
        email=f"{name.lower()}-{datetime.utcnow().timestamp()}@test.local",
        name=name,
        role=role,
        hashed_password="x",
        is_active=True,
    )
    db.add(u)
    db.flush()
    return u


@pytest.fixture
def world(db):
    """Azamat owns the group; Nuray is a colleague; one student is enrolled."""
    azamat = _user(db, "Azamat")
    nuray = _user(db, "Nuray")
    student = _user(db, "Student", role="student")

    group = Group(name="June 15 SAT - Azamat", teacher_id=azamat.id, is_active=True)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id,
                        created_at=datetime.utcnow() - timedelta(days=90)))
    db.flush()

    def lesson(*, when: datetime, teacher_id: int, minutes: int = 60, title="Lesson 32"):
        ev = Event(
            title=title, event_type="class",
            start_datetime=when, end_datetime=when + timedelta(minutes=minutes),
            teacher_id=teacher_id, created_by=azamat.id, is_active=True,
        )
        db.add(ev)
        db.flush()
        db.add(EventGroup(event_id=ev.id, group_id=group.id))
        db.flush()
        return ev

    def approve(event_id: int, substitute_id: int, status: str = "approved",
                confirmed_id: int | None = None):
        lr = LessonRequest(
            request_type="substitution", status=status,
            requester_id=azamat.id, event_id=event_id, group_id=group.id,
            original_datetime=datetime.utcnow(),
            substitute_teacher_id=substitute_id, confirmed_teacher_id=confirmed_id,
            resolved_by=azamat.id, resolved_at=datetime.utcnow(),
        )
        db.add(lr)
        db.flush()
        return lr

    def mark(event_id: int, status: str = "present"):
        db.add(Attendance(event_id=event_id, user_id=student.id, status=status))
        db.flush()

    return {
        "db": db, "azamat": azamat, "nuray": nuray, "student": student,
        "group": group, "lesson": lesson, "approve": approve, "mark": mark,
    }


# ------------------------------------------------------------------ 1. approval assigns


def test_approving_a_substitution_moves_the_lesson_to_the_substitute(world):
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    from src.lesson_requests.helpers import apply_approved_request

    ev = world["lesson"](when=datetime.utcnow() + timedelta(days=2), teacher_id=azamat.id)
    lr = world["approve"](ev.id, nuray.id)

    apply_approved_request(db, lr, resolver_id=azamat.id)
    db.flush()

    assert db.get(Event, ev.id).teacher_id == nuray.id
    assert db.get(Group, world["group"].id).teacher_id == azamat.id, \
        "ownership of the group does not move"


def test_applying_an_approved_substitution_is_idempotent(world):
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    from src.lesson_requests.helpers import apply_approved_request

    ev = world["lesson"](when=datetime.utcnow() + timedelta(days=2), teacher_id=azamat.id)
    lr = world["approve"](ev.id, nuray.id)

    for _ in range(3):
        apply_approved_request(db, lr, resolver_id=azamat.id)
        db.flush()
        assert db.get(Event, ev.id).teacher_id == nuray.id


# ------------------------------------------------- 2. every regeneration path preserves it


def _reconcile(db, group, when, teacher_id, minutes=60):
    from src.services.schedule_reconciliation import reconcile_group_schedule

    return reconcile_group_schedule(
        db=db, group_id=group.id, desired_slots=[(when, 1)],
        group_name=group.name, teacher_id=teacher_id, created_by=teacher_id,
    )


def test_schedule_reconciliation_preserves_the_substitute(world):
    db, azamat, nuray, group = world["db"], world["azamat"], world["nuray"], world["group"]
    when = datetime.utcnow() + timedelta(days=3)

    ev = world["lesson"](when=when, teacher_id=nuray.id)
    world["approve"](ev.id, nuray.id)

    _reconcile(db, group, when, azamat.id)
    db.flush()

    assert db.get(Event, ev.id).teacher_id == nuray.id


def test_reconciliation_restores_an_override_a_previous_run_had_lost(world):
    """Idempotent repair — and re-running does not oscillate."""
    db, azamat, nuray, group = world["db"], world["azamat"], world["nuray"], world["group"]
    when = datetime.utcnow() + timedelta(days=3)

    ev = world["lesson"](when=when, teacher_id=azamat.id)  # already corrupted
    world["approve"](ev.id, nuray.id)

    for _ in range(2):
        _reconcile(db, group, when, azamat.id)
        db.flush()
        assert db.get(Event, ev.id).teacher_id == nuray.id


def test_a_time_shift_and_renumbering_preserve_the_substitute(world):
    """The lesson moves day and is retitled; the occurrence override travels with it."""
    db, azamat, nuray, group = world["db"], world["azamat"], world["nuray"], world["group"]
    original = datetime.utcnow() + timedelta(days=3)
    moved = datetime.utcnow() + timedelta(days=4)

    ev = world["lesson"](when=original, teacher_id=nuray.id)
    world["approve"](ev.id, nuray.id)

    _reconcile(db, group, moved, azamat.id)
    db.flush()

    refreshed = db.get(Event, ev.id)
    assert refreshed.teacher_id == nuray.id
    assert refreshed.start_datetime.replace(microsecond=0) == moved.replace(microsecond=0)
    assert refreshed.title.endswith("Lesson 1"), "renumbering happened and did not disturb it"


def test_changing_the_groups_regular_teacher_does_not_erase_the_override(world):
    """A brand-new regular teacher inherits the group's ordinary lessons, not the covered one."""
    db, azamat, nuray, group = world["db"], world["azamat"], world["nuray"], world["group"]
    from src.services.schedule_reconciliation import sync_future_lesson_teachers

    newcomer = _user(db, "Newcomer")
    covered = world["lesson"](when=datetime.utcnow() + timedelta(days=3), teacher_id=nuray.id)
    ordinary = world["lesson"](when=datetime.utcnow() + timedelta(days=5), teacher_id=azamat.id,
                               title="Lesson 33")
    world["approve"](covered.id, nuray.id)

    sync_future_lesson_teachers(db, group.id, newcomer.id)
    db.flush()
    db.expire_all()

    assert db.get(Event, covered.id).teacher_id == nuray.id
    assert db.get(Event, ordinary.id).teacher_id == newcomer.id


def test_a_pending_request_does_not_pin_anybody(world):
    db, azamat, nuray, group = world["db"], world["azamat"], world["nuray"], world["group"]
    when = datetime.utcnow() + timedelta(days=3)

    ev = world["lesson"](when=when, teacher_id=azamat.id)
    world["approve"](ev.id, nuray.id, status="pending")

    _reconcile(db, group, when, azamat.id)
    db.flush()

    assert db.get(Event, ev.id).teacher_id == azamat.id


# ------------------------------------------------------------- 3. attendance ownership


def _unmarked_for(db, teacher_id):
    from src.admin.routes.dashboard import _missing_attendance_reminders

    return _missing_attendance_reminders(db, teacher_id=teacher_id)


def test_the_substitute_owes_the_register_and_the_group_owner_does_not(world):
    """The heart of the incident: the queue follows who taught, not who owns."""
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    # Yesterday, so it is a completed lesson awaiting its register. Another lesson in the
    # group was marked recently, which is what makes the group "actively using attendance".
    world["mark"](world["lesson"](
        when=datetime.utcnow() - timedelta(days=2), teacher_id=azamat.id, title="Lesson 31"
    ).id)
    covered = world["lesson"](when=datetime.utcnow() - timedelta(days=1), teacher_id=nuray.id)
    world["approve"](covered.id, nuray.id)
    db.flush()

    nuray_queue = {r["event_id"] for r in _unmarked_for(db, nuray.id)}
    azamat_queue = {r["event_id"] for r in _unmarked_for(db, azamat.id)}

    assert covered.id in nuray_queue, "Nuray taught it, so Nuray owes the register"
    assert covered.id not in azamat_queue, "it must be gone from Azamat's queue"


def test_a_lesson_with_no_recorded_teacher_still_falls_to_the_group_owner(world):
    """Legacy rows predating per-event teachers must not go ownerless."""
    db, azamat = world["db"], world["azamat"]
    world["mark"](world["lesson"](
        when=datetime.utcnow() - timedelta(days=2), teacher_id=azamat.id, title="Lesson 31"
    ).id)
    legacy = world["lesson"](when=datetime.utcnow() - timedelta(days=1), teacher_id=azamat.id)
    legacy.teacher_id = None
    db.flush()

    assert legacy.id in {r["event_id"] for r in _unmarked_for(db, azamat.id)}


def test_the_group_owner_cannot_mark_a_lesson_he_did_not_teach(world):
    """Hiding it in the UI is not access control — the server refuses."""
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    from src.utils.permissions import can_mark_event_attendance

    covered = world["lesson"](when=datetime.utcnow() - timedelta(days=1), teacher_id=nuray.id)
    world["approve"](covered.id, nuray.id)
    db.flush()

    assert can_mark_event_attendance(covered, nuray, db) is True
    assert can_mark_event_attendance(covered, azamat, db) is False


def test_an_admin_may_still_correct_any_register(world):
    db, nuray = world["db"], world["nuray"]
    from src.utils.permissions import can_mark_event_attendance

    admin = _user(db, "Admin", role="admin")
    head = _user(db, "Head", role="head_teacher")
    curator = _user(db, "Curator", role="curator")
    covered = world["lesson"](when=datetime.utcnow() - timedelta(days=1), teacher_id=nuray.id)
    db.flush()

    assert can_mark_event_attendance(covered, admin, db) is True
    assert can_mark_event_attendance(covered, head, db) is True
    assert can_mark_event_attendance(covered, curator, db) is False


# ------------------------------------------------------------------------- 4. the money


def _salary(db, teacher, start, end):
    from src.admin.routes.dashboard import get_teacher_salary_breakdown

    return get_teacher_salary_breakdown(
        period_start=start.strftime("%Y-%m-%d"),
        period_end=end.strftime("%Y-%m-%d"),
        lesson_rate=None,
        current_user=teacher,
        db=db,
    )


def test_an_unmarked_substituted_lesson_is_on_nobody_s_payslip(world):
    """Before the register is taken it is neither teacher's money — but it is Nuray's work."""
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    yesterday = datetime.utcnow() - timedelta(days=1)

    covered = world["lesson"](when=yesterday, teacher_id=nuray.id)
    world["approve"](covered.id, nuray.id)
    db.flush()

    window = (yesterday - timedelta(days=1), datetime.utcnow())
    azamat_pay = _salary(db, azamat, *window)
    nuray_pay = _salary(db, nuray, *window)

    assert azamat_pay["total_lessons"] == 0
    assert nuray_pay["total_lessons"] == 0
    assert nuray_pay["total_amount_tenge"] == 0
    # It is visible to Nuray as work awaiting a register, with a reason in Russian.
    assert nuray_pay["pending_lessons"] == 1
    assert azamat_pay["pending_lessons"] == 0
    assert "посещаемость" in nuray_pay["pending_note"].lower()


def test_once_marked_the_substituted_lesson_is_paid_only_to_the_substitute(world):
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    yesterday = datetime.utcnow() - timedelta(days=1)

    covered = world["lesson"](when=yesterday, teacher_id=nuray.id)
    world["approve"](covered.id, nuray.id)
    world["mark"](covered.id)
    db.flush()

    window = (yesterday - timedelta(days=1), datetime.utcnow())
    azamat_pay = _salary(db, azamat, *window)
    nuray_pay = _salary(db, nuray, *window)

    assert nuray_pay["total_lessons"] == 1
    assert nuray_pay["total_amount_tenge"] > 0
    assert nuray_pay["pending_lessons"] == 0
    assert azamat_pay["total_lessons"] == 0
    assert azamat_pay["total_amount_tenge"] == 0
    # Labelled as cover, and the group is still Azamat's.
    assert nuray_pay["groups"][0]["is_substitution"] is True
    assert db.get(Group, world["group"].id).teacher_id == azamat.id


def test_the_salary_period_is_bounded_by_almaty_midnight(world):
    """A lesson at 01:00 Almaty on the 1st is 20:00 UTC on the last day of the month before.

    A UTC-bounded period puts it in the wrong payslip.
    """
    db, azamat = world["db"], world["azamat"]
    from datetime import date

    # 2026-03-01 01:00 Almaty == 2026-02-28 20:00 UTC (stored naive-UTC).
    stored = datetime(2026, 2, 28, 20, 0)
    ev = world["lesson"](when=stored, teacher_id=azamat.id)
    world["mark"](ev.id)
    db.flush()

    march = _salary(db, azamat, date(2026, 3, 1), date(2026, 3, 31))
    february = _salary(db, azamat, date(2026, 2, 1), date(2026, 2, 28))

    assert march["total_lessons"] == 1, "it is 01:00 on the 1st where the school is"
    assert february["total_lessons"] == 0


# ------------------------------------------------------------------- 5. the repair command


def test_the_repair_dry_run_changes_nothing(world):
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    from src.services.substitution_repair import scan

    ev = world["lesson"](when=datetime.utcnow() + timedelta(days=3), teacher_id=azamat.id)
    world["approve"](ev.id, nuray.id)
    db.flush()

    report = scan(db, event_id=ev.id)

    assert len(report.mismatched) == 1
    assert report.mismatched[0].approved_teacher_id == nuray.id
    assert report.mismatched[0].current_teacher_id == azamat.id
    assert db.get(Event, ev.id).teacher_id == azamat.id, "a dry run writes nothing"


def test_the_repair_fixes_a_mismatch_once_and_is_idempotent(world):
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    from src.services.substitution_repair import repair, scan

    ev = world["lesson"](when=datetime.utcnow() + timedelta(days=3), teacher_id=azamat.id)
    world["approve"](ev.id, nuray.id)
    db.flush()

    first = repair(db, event_id=ev.id, actor_id=azamat.id)
    assert len(first.repaired) == 1
    assert db.get(Event, ev.id).teacher_id == nuray.id

    second = repair(db, event_id=ev.id, actor_id=azamat.id)
    assert second.repaired == []
    assert len(scan(db, event_id=ev.id).correct) == 1


def test_the_repair_refuses_to_choose_between_conflicting_approvals(world):
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    from src.services.substitution_repair import repair

    other = _user(db, "Other")
    ev = world["lesson"](when=datetime.utcnow() + timedelta(days=3), teacher_id=azamat.id)
    world["approve"](ev.id, nuray.id)
    world["approve"](ev.id, other.id)
    db.flush()

    report = repair(db, event_id=ev.id, actor_id=azamat.id)

    assert report.repaired == []
    assert len(report.conflicting) == 2
    assert db.get(Event, ev.id).teacher_id == azamat.id, "left exactly as found"


def test_the_repair_reports_an_already_marked_lesson_loudly(world):
    """Attendance was taken by the wrong teacher — somebody has to check the money."""
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    from src.services.substitution_repair import scan

    ev = world["lesson"](when=datetime.utcnow() - timedelta(days=1), teacher_id=azamat.id)
    world["approve"](ev.id, nuray.id)
    world["mark"](ev.id)
    db.flush()

    report = scan(db, event_id=ev.id)

    assert len(report.marked_affected) == 1
    assert report.summary()["mismatched_with_attendance_marked"] == 1


def test_the_repair_never_touches_pending_or_rejected_requests(world):
    db, azamat, nuray = world["db"], world["azamat"], world["nuray"]
    from src.services.substitution_repair import repair

    pending = world["lesson"](when=datetime.utcnow() + timedelta(days=3), teacher_id=azamat.id)
    rejected = world["lesson"](when=datetime.utcnow() + timedelta(days=4), teacher_id=azamat.id)
    world["approve"](pending.id, nuray.id, status="pending")
    world["approve"](rejected.id, nuray.id, status="rejected")
    db.flush()

    report = repair(db, actor_id=azamat.id)

    assert all(f.event_id not in (pending.id, rejected.id) for f in report.repaired)
    assert db.get(Event, pending.id).teacher_id == azamat.id
    assert db.get(Event, rejected.id).teacher_id == azamat.id
