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


def test_a_late_teacher_owes_200_a_minute_for_the_day(db, lesson_factory, meet_stub, teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 3), last_leave=datetime(2026, 9, 17, 14, 0))
    row = _row_of(service.register(db, SEPTEMBER, now=NOW), teacher.id)
    assert row["days"]["2026-09-17"]["late_minutes"] == 3
    assert row["days"]["2026-09-17"]["fine"] == 600
    assert row["totals"]["fine"] == 600
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
    assert closed.totals["fine"] == 400

    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 9), last_leave=datetime(2026, 9, 17, 14, 0))
    register = service.register(db, SEPTEMBER, now=NOW)
    assert register["totals"]["fine"] == 400  # a paid period does not move
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
    assert register["totals"]["fine"] == 600      # 3 minutes of hers; his 5 are somebody else's row


def _row_of(register: dict, teacher_id: int) -> dict:
    return next(row for row in register["teachers"] if row["teacher_id"] == teacher_id)


def _manages(db, head, group):
    """Link a head teacher to the course a group is taught under, as the LMS does."""
    from src.schemas.models import Course, CourseGroupAccess, CourseHeadTeacher
    course = Course(title=f"Course {datetime.now().timestamp():.6f}", description="", teacher_id=head.id)
    db.add(course); db.flush()
    db.add(CourseGroupAccess(course_id=course.id, group_id=group.id, granted_by=head.id, is_active=True))
    db.add(CourseHeadTeacher(course_id=course.id, head_teacher_id=head.id))
    db.flush()
    return course


def test_a_head_teacher_reaches_the_teachers_of_their_own_courses(db, lesson_factory, head_teacher, teacher):
    """Everywhere else in the LMS a head teacher is scoped to the courses they manage; the register
    scopes the same way, so the NUET head does not fine SAT teachers (owner, 2026-09-18)."""
    from src.schemas.models import EventGroup
    mine = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    other = _person(db, "teacher", "Другой преподаватель")
    theirs = lesson_factory(start=datetime(2026, 9, 17, 13, 0), of_teacher=other)

    group_of_mine = db.query(EventGroup).filter_by(event_id=mine.id).one().group_id
    _manages(db, head_teacher, db.get(__import__("src.schemas.models", fromlist=["Group"]).Group, group_of_mine))

    scope = service.teachers_of(db, head_teacher)
    assert teacher.id in scope                 # teaches a group of a course they manage
    assert other.id not in scope               # somebody else's course
    assert head_teacher.id in scope            # a head teacher who also teaches keeps their own row


def test_a_head_teacher_who_manages_nothing_still_sees_their_own_lessons(db, head_teacher):
    assert service.teachers_of(db, head_teacher) == [head_teacher.id]


def test_an_admin_is_not_scoped(db, head_teacher):
    admin = _person(db, "admin", "Админ")
    assert service.teachers_of(db, admin) is None


def test_the_programmes_of_a_period_do_not_shrink_when_one_is_chosen(db, lesson_factory, meet_stub, teacher):
    """The page's tabs come from this list. Deriving them from the filtered rows made every tab —
    including «All» — disappear as soon as a programme was chosen, with no way back."""
    sat = lesson_factory(start=datetime(2026, 9, 17, 13, 0), program="SAT")
    ielts = lesson_factory(start=datetime(2026, 9, 17, 15, 0), program="IELTS",
                           of_teacher=_person(db, "teacher", "IELTS преподаватель"))
    for lesson in (sat, ielts):
        meet_stub(lesson, first_join=lesson.start_datetime, last_leave=lesson.end_datetime)

    everything = service.register(db, SEPTEMBER, now=NOW)
    assert everything["programs"] == ["IELTS", "SAT"]

    only_sat = service.register(db, SEPTEMBER, program="SAT", now=NOW)
    assert only_sat["programs"] == ["IELTS", "SAT"]          # the choice does not shrink the choices
    assert [row["program"] for row in only_sat["teachers"]] == ["SAT"]


def test_a_substitute_on_a_managed_course_is_in_the_register(db, lesson_factory, meet_stub, head_teacher):
    """Scope follows the lesson, not who owns the group.

    Asking «which groups does this teacher own» lost every substitute: on production Қайратқызы
    Дина taught six NUET lessons of somebody else's group and no head teacher could see her, only
    admins (2026-09-18). A head teacher sees the lessons of the courses they manage, whoever taught.
    """
    from src.schemas.models import EventGroup, Group
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    group_id = db.query(EventGroup).filter_by(event_id=lesson.id).one().group_id
    _manages(db, head_teacher, db.get(Group, group_id))

    substitute = _person(db, "teacher", "Подменяющий преподаватель")
    lesson.teacher_id = substitute.id            # taught by somebody who owns no group
    db.flush()
    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 4), last_leave=datetime(2026, 9, 17, 14, 0))

    register = service.register(db, SEPTEMBER, viewer=head_teacher, now=NOW)
    assert [row["teacher_id"] for row in register["teachers"]] == [substitute.id]
    assert register["totals"]["fine"] == 800


def test_a_head_teacher_does_not_see_another_courses_lesson(db, lesson_factory, meet_stub, head_teacher, teacher):
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 3), last_leave=datetime(2026, 9, 17, 14, 0))
    assert service.register(db, SEPTEMBER, viewer=head_teacher, now=NOW)["teachers"] == []


def test_a_teacher_sees_their_own_lessons_whoever_owns_the_group(db, lesson_factory, meet_stub, teacher):
    mine = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    other = _person(db, "teacher", "Другой")
    theirs = lesson_factory(start=datetime(2026, 9, 17, 15, 0), of_teacher=other)
    for lesson in (mine, theirs):
        meet_stub(lesson, first_join=lesson.start_datetime + timedelta(minutes=3),
                  last_leave=lesson.end_datetime)
    register = service.register(db, SEPTEMBER, viewer=teacher, now=NOW)
    assert [row["teacher_id"] for row in register["teachers"]] == [teacher.id]


def test_an_admin_sees_every_lesson(db, lesson_factory, meet_stub, teacher):
    admin = _person(db, "admin", "Админ")
    lesson = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(lesson, first_join=datetime(2026, 9, 17, 13, 3), last_leave=datetime(2026, 9, 17, 14, 0))
    assert len(service.register(db, SEPTEMBER, viewer=admin, now=NOW)["teachers"]) == 1


def test_a_head_teacher_may_touch_only_their_courses_lessons(db, lesson_factory, head_teacher):
    from src.schemas.models import EventGroup, Group
    mine = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    theirs = lesson_factory(start=datetime(2026, 9, 17, 15, 0))
    group_id = db.query(EventGroup).filter_by(event_id=mine.id).one().group_id
    _manages(db, head_teacher, db.get(Group, group_id))

    assert service.may_touch_lesson(db, head_teacher, mine.id) is True
    assert service.may_touch_lesson(db, head_teacher, theirs.id) is False


def test_the_grid_says_how_many_late_minutes_were_made_up(db, lesson_factory, meet_stub, teacher):
    """A head teacher must see «отработано» on the grid, not only inside the day panel.

    The fine still stands — only a person may waive it — but three minutes a teacher gave
    back at the end of the lesson read differently from three minutes nobody returned, and
    that difference is what the head teacher is deciding on.
    """
    made_up = lesson_factory(start=datetime(2026, 9, 17, 13, 0))
    meet_stub(made_up, first_join=datetime(2026, 9, 17, 13, 3),
              last_leave=datetime(2026, 9, 17, 14, 3))
    plain = lesson_factory(start=datetime(2026, 9, 18, 13, 0))
    meet_stub(plain, first_join=datetime(2026, 9, 18, 13, 2),
              last_leave=datetime(2026, 9, 18, 14, 0))

    row = _row_of(service.register(db, SEPTEMBER, now=NOW), teacher.id)
    assert row["days"]["2026-09-17"]["made_up_minutes"] == 3
    assert row["days"]["2026-09-17"]["late_minutes"] == 3      # the fine is unchanged
    assert row["days"]["2026-09-17"]["fine"] == 600
    assert row["days"]["2026-09-18"]["made_up_minutes"] == 0
    assert row["totals"]["made_up_minutes"] == 3
