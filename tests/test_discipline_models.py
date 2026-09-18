"""A row exists only where a person decided something; measurements stay in the Meet record."""
from datetime import date, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from src.discipline.models import DisciplineDecision, DisciplinePeriod


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


def _person(db, role="teacher"):
    from src.schemas.models import UserInDB
    from src.utils.auth_utils import hash_password
    user = UserInDB(email=f"td-{datetime.now().timestamp():.6f}-{role}@test.local", name=f"{role} person",
                    role=role, hashed_password=hash_password("x"), is_active=True)
    db.add(user)
    db.flush()
    return user


def test_a_decision_records_who_priced_what(db):
    teacher, head = _person(db), _person(db, "head_teacher")
    db.add(DisciplineDecision(event_id=None, teacher_id=teacher.id, day=date(2026, 9, 17),
                              kind="miss", minutes=60, proposed_amount=None, amount=15000,
                              reason_code="other", note="не вышел, класс ждал",
                              decided_by=head.id, decided_at=datetime.utcnow()))
    db.flush()
    saved = db.query(DisciplineDecision).filter_by(teacher_id=teacher.id).one()
    assert (saved.amount, saved.kind, saved.decided_by) == (15000, "miss", head.id)
    assert saved.event_id is None  # something Meet never saw, entered by hand


def _lesson(db, teacher):
    from src.schemas.models import Event
    event = Event(title="SAT, урок 7", event_type="class", start_datetime=datetime(2026, 9, 17, 13, 0),
                  end_datetime=datetime(2026, 9, 17, 14, 0), created_by=teacher.id,
                  teacher_id=teacher.id, is_active=True)
    db.add(event)
    db.flush()
    return event


def test_one_decision_per_lesson_and_kind(db):
    teacher, head = _person(db), _person(db, "head_teacher")
    lesson = _lesson(db, teacher)
    for _ in range(2):
        db.add(DisciplineDecision(event_id=lesson.id, teacher_id=teacher.id, day=date(2026, 9, 17),
                                  kind="late", minutes=3, proposed_amount=900, amount=0,
                                  reason_code="moved", decided_by=head.id,
                                  decided_at=datetime.utcnow()))
    with pytest.raises(IntegrityError):
        db.flush()


def test_the_same_lesson_can_owe_for_lateness_and_for_ending_early(db):
    teacher, head = _person(db), _person(db, "head_teacher")
    lesson = _lesson(db, teacher)
    for kind, minutes in (("late", 2), ("ended_early", 10)):
        db.add(DisciplineDecision(event_id=lesson.id, teacher_id=teacher.id, day=date(2026, 9, 17),
                                  kind=kind, minutes=minutes, proposed_amount=minutes * 200,
                                  amount=minutes * 200, decided_by=head.id,
                                  decided_at=datetime.utcnow()))
    db.flush()
    assert db.query(DisciplineDecision).filter_by(event_id=lesson.id).count() == 2


def test_a_closed_period_keeps_its_numbers(db):
    head = _person(db, "head_teacher")
    db.add(DisciplinePeriod(period_key="2026-09-16", starts_on=date(2026, 9, 16),
                            ends_on=date(2026, 9, 30), closed_at=datetime.utcnow(),
                            closed_by=head.id, totals={"fine": 13200, "late_minutes": 44}))
    db.flush()
    saved = db.query(DisciplinePeriod).filter_by(period_key="2026-09-16").one()
    assert saved.totals["fine"] == 13200 and saved.closed_by == head.id
