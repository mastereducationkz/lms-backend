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


from datetime import datetime, timezone

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
