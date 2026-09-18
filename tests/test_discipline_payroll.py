"""What the LMS's own salary pages are told a teacher owes.

The CRM reads the same numbers over HTTP. Both sides end at `judged_lessons`, and this file
exists to keep it that way: a teacher reading their LMS salary breakdown and an accountant
reading the CRM must never be shown different money for the same lessons.
"""
from datetime import date, datetime, timedelta

import pytest

from src.discipline import payroll, service
from src.discipline.rules import period_containing

NOW = datetime(2026, 10, 1, 0, 0)


@pytest.fixture
def db():
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession

    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


def _person(db, role, name):
    from src.schemas.models import UserInDB
    from src.utils.auth_utils import hash_password
    user = UserInDB(email=f"pay-{datetime.now().timestamp():.6f}-{role}@test.local", name=name,
                    role=role, hashed_password=hash_password("x"), is_active=True)
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def world(db, monkeypatch):
    from src.schemas.models import Event, EventGroup, Group, GroupStudent
    teacher = _person(db, "teacher", "Педагог")
    other = _person(db, "teacher", "Другой педагог")
    head = _person(db, "head_teacher", "Завуч")
    group = Group(name="SAT July 16", is_active=True, is_over=False, program_type="SAT",
                  teacher_id=teacher.id)
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=_person(db, "student", "Ученик").id))

    timings = {}

    def lesson(*, day, hour=8, minutes=60, late=0, early=0, made_up=False, miss=False,
               who=None):
        start = datetime(2026, 9, day, hour, 0)
        end = start + timedelta(minutes=minutes)
        event = Event(title="SAT", event_type="class", start_datetime=start, end_datetime=end,
                      created_by=teacher.id, teacher_id=(who or teacher).id, is_active=True)
        db.add(event)
        db.flush()
        db.add(EventGroup(event_id=event.id, group_id=group.id))
        db.flush()
        if miss:
            timings[event.id] = ("ready", None, None, 0, 0)
        else:
            leave = end - timedelta(minutes=early)
            if made_up:
                leave = end + timedelta(minutes=late)
            timings[event.id] = ("ready", start + timedelta(minutes=late), leave, 8, 9)
        return event

    monkeypatch.setattr(service, "_timings", lambda db_, events, now: {
        e.id: timings.get(e.id, ("no_room", None, None, 0, 0)) for e in events})
    return {"teacher": teacher, "other": other, "head": head, "lesson": lesson}


def test_a_range_is_summed_over_the_half_months_it_touches(db, world):
    world["lesson"](day=17, late=3)                       # 16–30 September
    world["lesson"](day=18, early=2, hour=10)
    fines = payroll.fines_for(db, world["teacher"].id, date(2026, 9, 16), date(2026, 9, 30),
                              now=NOW)
    assert fines.total == 1000                            # (3 + 2) × 200
    assert (fines.late_minutes, fines.early_minutes) == (3, 2)
    assert fines.any is True


def test_only_the_days_asked_for_are_counted(db, world):
    """A payslip for the second half of the month must not pick up the first half's minutes."""
    world["lesson"](day=17, late=3)
    world["lesson"](day=25, late=1, hour=11)
    fines = payroll.fines_for(db, world["teacher"].id, date(2026, 9, 20), date(2026, 9, 30),
                              now=NOW)
    assert fines.late_minutes == 1
    assert fines.total == 200


def test_another_teachers_lesson_is_never_on_this_payslip(db, world):
    world["lesson"](day=17, late=5, who=world["other"])
    assert payroll.fines_for(db, world["teacher"].id, date(2026, 9, 16), date(2026, 9, 30),
                             now=NOW).total == 0


def test_a_waived_fine_stops_counting(db, world):
    lesson = world["lesson"](day=17, late=3)
    service.apply_decision(db, actor=world["head"], event_id=lesson.id,
                           teacher_id=world["teacher"].id, day=date(2026, 9, 17), kind="late",
                           amount=0, reason_code="moved")
    fines = payroll.fines_for(db, world["teacher"].id, date(2026, 9, 16), date(2026, 9, 30),
                              now=NOW)
    assert fines.total == 0
    assert fines.late_minutes == 3          # what happened is still reported


def test_made_up_minutes_are_reported_but_still_cost(db, world):
    """Only a head teacher may waive. The number simply travels with its context."""
    world["lesson"](day=17, late=3, made_up=True)
    fines = payroll.fines_for(db, world["teacher"].id, date(2026, 9, 16), date(2026, 9, 30),
                              now=NOW)
    assert fines.made_up_minutes == 3
    assert fines.total == 600


def test_an_unpriced_miss_is_counted_as_waiting_not_as_zero(db, world):
    world["lesson"](day=17, miss=True)
    fines = payroll.fines_for(db, world["teacher"].id, date(2026, 9, 16), date(2026, 9, 30),
                              now=NOW)
    assert (fines.misses, fines.unpriced, fines.total) == (1, 1, 0)
    assert fines.any is True                # there IS something to say, just no number yet


def test_an_open_period_is_never_final(db, world):
    world["lesson"](day=17, late=3)
    assert payroll.fines_for(db, world["teacher"].id, date(2026, 9, 16), date(2026, 9, 30),
                             now=NOW).final is False


def test_a_closed_period_is_final(db, world):
    world["lesson"](day=17, late=3)
    service.close_period(db, period_containing(date(2026, 9, 17)), world["head"], now=NOW)
    assert payroll.fines_for(db, world["teacher"].id, date(2026, 9, 16), date(2026, 9, 30),
                             now=NOW).final is True


def test_a_range_entirely_before_the_rule_is_simply_empty(db, world):
    fines = payroll.fines_for(db, world["teacher"].id, date(2026, 8, 1), date(2026, 8, 31),
                              now=NOW)
    assert fines == payroll.Fines()
    assert fines.any is False


def test_a_range_starting_before_the_rule_begins_at_the_rule(db, world):
    world["lesson"](day=17, late=3)
    assert payroll.periods_between(date(2026, 9, 1), date(2026, 9, 30))[0].start == \
        date(2026, 9, 16)
