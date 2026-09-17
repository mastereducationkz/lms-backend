"""The register: which lessons it judges, what a day costs, and whose decision wins.

The Meet record is stubbed (`meet_stub`) so these tests never call Google: what matters here is
which lessons enter the register, how a day is summed, and that a person's decision beats the rule.
"""
from datetime import date, datetime, timedelta

import pytest

from src.discipline import service
from src.discipline.rules import period_containing

SEPTEMBER = period_containing(date(2026, 9, 17))
NOW = datetime(2026, 10, 1, 0, 0)  # every lesson below has finished by then


@pytest.fixture
def db():
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as SASession
    from src.config import engine
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")
    trans = connection.begin()
    session = SASession(bind=connection)
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart(sess, transaction):
        if transaction.nested and not transaction._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart)
        session.close()
        trans.rollback()
        connection.close()


def _person(db, role, name):
    from src.schemas.models import UserInDB
    from src.utils.auth_utils import hash_password
    user = UserInDB(email=f"ds-{datetime.now().timestamp():.6f}-{role}@test.local", name=name,
                    role=role, hashed_password=hash_password("x"), is_active=True)
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def teacher(db):
    return _person(db, "teacher", "Кенжебаев Арсен")


@pytest.fixture
def head_teacher(db):
    return _person(db, "head_teacher", "Head Teacher")


@pytest.fixture
def lesson_factory(db, teacher):
    from src.schemas.models import Event, EventGroup, Group, GroupStudent

    def make(start: datetime, minutes: int = 60, program: str = "SAT", of_teacher=None):
        owner = of_teacher or teacher
        group = Group(name=f"{program} group {datetime.now().timestamp():.6f}", is_active=True,
                      is_over=False, program_type=program, teacher_id=owner.id)
        db.add(group)
        db.flush()
        # An operational group is one with at least one active student on it.
        db.add(GroupStudent(group_id=group.id, student_id=_person(db, "student", "Ученик").id))
        db.flush()
        event = Event(title=f"{program}, урок", event_type="class", start_datetime=start,
                      end_datetime=start + timedelta(minutes=minutes), created_by=owner.id,
                      teacher_id=owner.id, is_active=True, meeting_url="https://meet.google.com/abc-defg-hij")
        db.add(event)
        db.flush()
        db.add(EventGroup(event_id=event.id, group_id=group.id))
        db.flush()
        return event

    return make


@pytest.fixture
def meet_stub(monkeypatch):
    """Stand in for the Meet record: {event_id: (state, first join, last leave)}."""
    timings: dict[int, tuple] = {}

    def stub(lesson, first_join=None, last_leave=None, state="ready", students_at_end=0, students=0):
        timings[lesson.id] = (state, first_join, last_leave, students_at_end, students)

    monkeypatch.setattr(service, "_timings", lambda db, events, now: {
        e.id: timings.get(e.id, ("no_room", None, None, 0, 0)) for e in events})
    return stub


def test_lessons_before_the_rule_started_are_not_in_the_register(db, lesson_factory):
    lesson_factory(start=datetime(2026, 9, 15, 13, 0))  # the day before the rule
    inside = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    ids = [e.id for e in service.lessons_in(db, SEPTEMBER, NOW)]
    assert inside.id in ids
    assert len(ids) == 1


def test_a_lesson_that_has_not_finished_is_not_in_the_register_yet(db, lesson_factory, meet_stub, teacher):
    """The open period runs to the end of the month: its later days have not happened.

    Without this, every lesson still to come counted as one the LMS «could not watch» — on
    18.09 that was 573 of 674 — and the grid painted the rest of the month as unmeasurable.
    """
    finished = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(finished, first_join=datetime(2026, 9, 17, 13, 0), last_leave=datetime(2026, 9, 17, 14, 0))
    lesson_factory(start=datetime(2026, 9, 25, 13, 0))   # still to come
    now = datetime(2026, 9, 18, 6, 0)
    assert [e.id for e in service.lessons_in(db, SEPTEMBER, now)] == [finished.id]

    register = service.register(db, SEPTEMBER, now=now)
    assert register["totals"]["lessons"] == 1
    assert register["totals"]["unmeasurable"] == 0


def test_a_lesson_running_right_now_waits_for_its_end(db, lesson_factory, teacher):
    lesson_factory(start=datetime(2026, 9, 18, 5, 0), minutes=60)
    now = datetime(2026, 9, 18, 5, 30)  # half way through
    assert service.lessons_in(db, SEPTEMBER, now) == []


def test_a_late_teacher_owes_300_a_minute_for_the_day(db, lesson_factory, meet_stub, teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 3), last_leave=datetime(2026, 9, 17, 14, 0))
    row = _row_of(service.register(db, SEPTEMBER, now=NOW), teacher.id)
    assert row["days"]["2026-09-17"]["late_minutes"] == 3
    assert row["days"]["2026-09-17"]["fine"] == 900
    assert row["totals"]["fine"] == 900
    assert row["program"] == "SAT"


def test_a_waived_fine_stops_counting_but_stays_visible(db, lesson_factory, meet_stub, teacher, head_teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 3), last_leave=datetime(2026, 9, 17, 14, 0))
    service.apply_decision(db, actor=head_teacher, event_id=lesson.id, teacher_id=teacher.id,
                           day=date(2026, 9, 17), kind="late", amount=0, reason_code="moved", note="")
    row = _row_of(service.register(db, SEPTEMBER, now=NOW), teacher.id)
    assert row["totals"]["fine"] == 0
    assert row["days"]["2026-09-17"]["late_minutes"] == 3  # the minutes still happened
    assert row["days"]["2026-09-17"]["decided"] == 1


def test_a_miss_waits_for_a_person_to_price_it(db, lesson_factory, meet_stub, teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=None, last_leave=None)
    row = _row_of(service.register(db, SEPTEMBER, now=NOW), teacher.id)
    assert row["days"]["2026-09-17"]["misses"] == 1
    assert row["days"]["2026-09-17"]["fine"] == 0
    assert row["totals"]["unpriced"] == 1


def test_a_priced_miss_counts_like_any_other_fine(db, lesson_factory, meet_stub, teacher, head_teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=None, last_leave=None)
    service.apply_decision(db, actor=head_teacher, event_id=lesson.id, teacher_id=teacher.id,
                           day=date(2026, 9, 17), kind="miss", amount=15000, reason_code=None, note="")
    row = _row_of(service.register(db, SEPTEMBER, now=NOW), teacher.id)
    assert (row["totals"]["fine"], row["totals"]["unpriced"]) == (15000, 0)


def test_a_lesson_without_a_meet_room_is_not_judged(db, lesson_factory, meet_stub, teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, state="no_room")
    cell = _row_of(service.register(db, SEPTEMBER, now=NOW), teacher.id)["days"]["2026-09-17"]
    assert (cell["state"], cell["unmeasurable"], cell["fine"]) == ("unmeasurable", 1, 0)


def test_a_day_shows_its_worst_news(db, lesson_factory, meet_stub, teacher):
    morning = lesson_factory(start=datetime(2026, 9, 17, 5, 0))
    evening = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(morning, first_join=datetime(2026, 9, 17, 5, 1), last_leave=datetime(2026, 9, 17, 6, 0))
    meet_stub(evening, first_join=None, last_leave=None)
    cell = _row_of(service.register(db, SEPTEMBER, now=NOW), teacher.id)["days"]["2026-09-17"]
    assert cell["state"] == "miss"
    assert cell["late_minutes"] == 1
    assert cell["lessons"] == 2


def test_the_day_panel_explains_a_made_up_lesson(db, lesson_factory, meet_stub, teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 3), last_leave=datetime(2026, 9, 17, 14, 3),
              students=9, students_at_end=8)
    detail = service.day_detail(db, teacher.id, date(2026, 9, 17), now=NOW)
    lesson_row = detail["lessons"][0]
    finding = lesson_row["findings"][0]
    assert (finding["kind"], finding["minutes"], finding["made_up"]) == ("late", 3, True)
    assert (lesson_row["students"], lesson_row["students_at_end"]) == (9, 8)
    assert lesson_row["group"].startswith("SAT")


def test_closing_a_period_freezes_its_totals(db, lesson_factory, meet_stub, teacher, head_teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 2), last_leave=datetime(2026, 9, 17, 14, 0))
    closed = service.close_period(db, SEPTEMBER, head_teacher, now=NOW)
    assert closed.totals["fine"] == 600

    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 9), last_leave=datetime(2026, 9, 17, 14, 0))
    register = service.register(db, SEPTEMBER, now=NOW)
    assert register["totals"]["fine"] == 600  # a paid period does not move
    assert register["period"]["closed"] is True


def test_a_period_with_an_unpriced_miss_cannot_be_closed(db, lesson_factory, meet_stub, head_teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=None, last_leave=None)
    with pytest.raises(service.PeriodNotReady):
        service.close_period(db, SEPTEMBER, head_teacher, now=NOW)


def test_one_teacher_sees_only_their_own_row(db, lesson_factory, meet_stub, teacher):
    other = _person(db, "teacher", "Другой преподаватель")
    mine = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    theirs = lesson_factory(start=datetime(2026, 9, 17, 13, 0), of_teacher=other)
    meet_stub(mine, first_join=datetime(2026, 9, 17, 13, 3), last_leave=datetime(2026, 9, 17, 14, 0))
    meet_stub(theirs, first_join=datetime(2026, 9, 17, 13, 5), last_leave=datetime(2026, 9, 17, 14, 0))
    register = service.register(db, SEPTEMBER, teacher_ids=[teacher.id], now=NOW)
    assert [row["teacher_id"] for row in register["teachers"]] == [teacher.id]
    assert register["totals"]["fine"] == 900


def _row_of(register: dict, teacher_id: int) -> dict:
    return next(row for row in register["teachers"] if row["teacher_id"] == teacher_id)
