"""Уважительный пропуск: инвариант, запись, чтение.

Уважительность — не статус, а флаг строки. Эти тесты закрепляют главное свойство:
словарь статусов от неё не меняется, поэтому ни проценты посещаемости, ни признак
«отмечено», ни биллинг не сдвигаются от самого факта её появления.
"""
import pytest

from src.services.attendance_status import (
    ABSENT_STATUSES,
    MARKED_STATUSES,
    PRESENT_STATUSES,
    is_excused,
    normalize_status,
    validate_excused,
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
        session.close(); trans.rollback(); connection.close()


def test_vocabulary_is_untouched_by_the_new_flag():
    """Если этот тест упал — значит в множества добавили значение, и вместе с ним
    поехали проценты, «отмечено» и списание урока с баланса."""
    assert PRESENT_STATUSES == frozenset({"present", "presented", "1", "yes", "late"})
    assert ABSENT_STATUSES == frozenset({"absent", "0", "no", "missed"})
    assert MARKED_STATUSES == PRESENT_STATUSES | ABSENT_STATUSES
    assert normalize_status("absent") == "absent"


def test_excused_needs_an_absent_status():
    with pytest.raises(ValueError) as exc:
        validate_excused("present", True, "заболел")
    assert str(exc.value) == "excused_requires_absent"


def test_excused_needs_a_non_empty_note():
    with pytest.raises(ValueError) as exc:
        validate_excused("absent", True, "   ")
    assert str(exc.value) == "excused_requires_note"


def test_a_valid_excused_row_passes():
    assert validate_excused("absent", True, "предупредил заранее") is None
    assert validate_excused("missed", True, "болел") is None


def test_an_unexcused_row_is_never_checked():
    """excused=false — обычный пропуск; причина не нужна и статус не проверяется."""
    assert validate_excused("present", False, None) is None
    assert validate_excused(None, False, None) is None


def test_is_excused_requires_both_halves():
    assert is_excused("absent", True) is True
    assert is_excused("absent", False) is False
    assert is_excused("present", True) is False


from datetime import datetime, timedelta, timezone

from sqlalchemy import text

# `src.schemas.models` first, deliberately — same pre-existing circular import documented in
# tests/test_attendance_future_lesson_guard.py: importing `src.events.models` before the model
# package has finished loading trips it.
from src.schemas.models import Event  # noqa: F401  isort: skip
from src.events.models import Attendance


def test_new_rows_default_to_unexcused(db):
    """История остаётся неуважительной: дефолт — это и есть принятое решение по ней."""
    row = Attendance(event_id=None, lesson_schedule_id=None, user_id=1, status="absent")
    # Ни один из новых атрибутов не задан вызывающим.
    assert row.excused in (False, None)
    assert row.excuse_note is None
    assert row.excused_by_user_id is None
    assert row.excused_at is None


def test_the_columns_exist_in_the_database(db):
    cols = {
        r[0]
        for r in db.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'attendances'"
            )
        ).fetchall()
    }
    assert {"excused", "excuse_note", "excused_by_user_id", "excused_at"} <= cols


@pytest.fixture()
def event_and_student(db):
    """Прошедший урок и один студент на нём — минимум, который принимает запись."""
    from src.events.models import Event
    from src.schemas.models import UserInDB

    student = UserInDB(
        name="Тест Студентов", email=f"exc-{datetime.now().timestamp()}@test.local",
        hashed_password="x", role="student",
    )
    teacher = UserInDB(
        name="Тест Учителев", email=f"exc-t-{datetime.now().timestamp()}@test.local",
        hashed_password="x", role="teacher",
    )
    db.add_all([student, teacher])
    db.flush()
    start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    event = Event(
        title="Урок", event_type="class", start_datetime=start,
        end_datetime=start + timedelta(hours=1), is_active=True,
        teacher_id=teacher.id, created_by=teacher.id,
    )
    db.add(event)
    db.flush()
    return event.id, student.id


from src.services.attendance_service import AttendanceService


def test_upsert_stores_the_excuse(db, event_and_student):
    event_id, user_id = event_and_student
    row = AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="предупредил заранее", excused_by_user_id=user_id,
    )
    assert row.excused is True
    assert row.excuse_note == "предупредил заранее"
    assert row.excused_at is not None


def test_moving_off_absent_clears_the_excuse(db, event_and_student):
    """Учитель переставил «Ув.» на «Был». Сетка шлёт новый статус и ничего про
    уважительность — строка не имеет права остаться уважительной при статусе present,
    иначе в базе появится то, что инвариант запрещает."""
    event_id, user_id = event_and_student
    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="болел",
    )
    row = AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="present",
    )
    assert row.excused is False
    assert row.excuse_note is None
    assert row.excused_at is None


def test_an_unaware_caller_does_not_wipe_the_excuse(db, event_and_student):
    """Пути, которые про уважительность не знают (перепривязка урока, импорт), не должны
    снимать её, пока статус остаётся пропуском."""
    event_id, user_id = event_and_student
    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="олимпиада",
    )
    row = AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent", score=0,
    )
    assert row.excused is True
    assert row.excuse_note == "олимпиада"


def test_upsert_refuses_an_invalid_excuse(db, event_and_student):
    event_id, user_id = event_and_student
    with pytest.raises(ValueError) as exc:
        AttendanceService.upsert_for_event(
            db, event_id=event_id, user_id=user_id, status="absent",
            excused=True, excuse_note="",
        )
    assert str(exc.value) == "excused_requires_note"


def test_the_map_carries_the_excuse(db, event_and_student):
    event_id, user_id = event_and_student
    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="семейные",
    )
    db.flush()
    m = AttendanceService.get_attendance_map_for_events(db, [event_id], [user_id])
    assert m[(user_id, event_id)]["excused"] is True
    assert m[(user_id, event_id)]["excuse_note"] == "семейные"
