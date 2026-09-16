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
from src.courses.models import Group


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


from fastapi import HTTPException

from src.events.routes.events import update_event_attendance
from src.events.schemas import AttendanceBulkUpdateSchema, AttendanceRecord


@pytest.fixture()
def teacher_group(db):
    """A `Group` for `/leaderboard/curator/attendance/bulk`'s auth check, which — unlike the
    events endpoint — authorises on `group.teacher_id`, not `event.teacher_id`:
    `update_attendance_bulk` looks the group up by `item.group_id` and skips the row unless
    `group.teacher_id == current_user.id`. Its own teacher, shared with `marking_teacher`,
    is created here rather than borrowed from `event_and_student`'s teacher so this fixture
    has no dependency on that one.
    """
    from src.schemas.models import UserInDB

    teacher = UserInDB(
        name="Замещающий Учитель", email=f"exc-mt-{datetime.now().timestamp()}@test.local",
        hashed_password="x", role="teacher",
    )
    db.add(teacher)
    db.flush()

    group = Group(name="Тестовая группа", teacher_id=teacher.id)
    db.add(group)
    db.flush()

    return group


@pytest.fixture()
def marking_teacher(db, event_and_student, teacher_group):
    """A teacher that both `check_event_access` and `can_mark_event_attendance` accept for
    the event from `event_and_student`, through the ordinary path: the event's own teacher
    marking their own lesson.

    `can_mark_event_attendance` short-circuits on `event.teacher_id == user.id` whenever that
    column is set (`src/utils/permissions.py`), never reaching its Group/EventGroup fallback —
    that fallback is only for legacy lessons that never recorded a teacher. Pointing
    `event.teacher_id` at this teacher hits that equality branch directly.
    `check_event_access`'s teacher branch also falls through to the same
    `event.teacher_id == user.id` check once its EventGroup/Course loops find nothing, so no
    Group or EventGroup rows are needed for either function here.

    Shares its identity with `teacher_group.teacher_id` so `update_attendance_bulk`'s
    group-based auth check accepts it too.
    """
    from src.schemas.models import UserInDB

    event_id, _ = event_and_student
    teacher = db.query(UserInDB).filter(UserInDB.id == teacher_group.teacher_id).first()

    event = db.query(Event).filter(Event.id == event_id).first()
    event.teacher_id = teacher.id
    db.flush()

    return teacher


def test_event_endpoint_stores_the_excuse(db, event_and_student, marking_teacher):
    event_id, user_id = event_and_student
    update_event_attendance(
        event_id,
        AttendanceBulkUpdateSchema(
            attendance=[
                AttendanceRecord(
                    student_id=user_id, status="missed",
                    excused=True, excuse_note="был на олимпиаде",
                )
            ]
        ),
        db,
        marking_teacher,
    )
    row = AttendanceService.get_by_event_and_user(db, event_id, user_id)
    assert row.status == "absent"
    assert row.excused is True
    assert row.excuse_note == "был на олимпиаде"
    assert row.excused_by_user_id == marking_teacher.id


def test_event_endpoint_refuses_an_excuse_without_a_note(db, event_and_student, marking_teacher):
    event_id, user_id = event_and_student
    with pytest.raises(HTTPException) as exc:
        update_event_attendance(
            event_id,
            AttendanceBulkUpdateSchema(
                attendance=[
                    AttendanceRecord(student_id=user_id, status="missed", excused=True)
                ]
            ),
            db,
            marking_teacher,
        )
    assert exc.value.status_code == 422
    assert "причин" in exc.value.detail.lower()


def test_event_endpoint_refuses_an_excused_present(db, event_and_student, marking_teacher):
    event_id, user_id = event_and_student
    with pytest.raises(HTTPException) as exc:
        update_event_attendance(
            event_id,
            AttendanceBulkUpdateSchema(
                attendance=[
                    AttendanceRecord(
                        student_id=user_id, status="attended",
                        excused=True, excuse_note="болел",
                    )
                ]
            ),
            db,
            marking_teacher,
        )
    assert exc.value.status_code == 422


from src.gamification.routes.leaderboard import (
    AttendanceInputSchema,
    BulkAttendanceInputSchema,
    update_attendance_bulk,
)


def test_grid_bulk_stores_the_excuse(db, event_and_student, marking_teacher, teacher_group):
    event_id, user_id = event_and_student
    update_attendance_bulk(
        BulkAttendanceInputSchema(
            updates=[
                AttendanceInputSchema(
                    group_id=teacher_group.id, week_number=1, lesson_index=1,
                    student_id=user_id, score=0, status="missed", event_id=event_id,
                    excused=True, excuse_note="болел",
                )
            ]
        ),
        marking_teacher,
        db,
    )
    row = AttendanceService.get_by_event_and_user(db, event_id, user_id)
    assert row.excused is True
    assert row.excuse_note == "болел"
    assert row.score == 0
    assert row.excused_by_user_id == marking_teacher.id


def test_grid_bulk_refuses_an_excuse_without_a_note(db, event_and_student, marking_teacher, teacher_group):
    event_id, user_id = event_and_student
    with pytest.raises(HTTPException) as exc:
        update_attendance_bulk(
            BulkAttendanceInputSchema(
                updates=[
                    AttendanceInputSchema(
                        group_id=teacher_group.id, week_number=1, lesson_index=1,
                        student_id=user_id, score=0, status="missed", event_id=event_id,
                        excused=True, excuse_note="  ",
                    )
                ]
            ),
            marking_teacher,
            db,
        )
    assert exc.value.status_code == 422


def test_grid_bulk_writes_nothing_when_one_row_is_invalid(
    db, event_and_student, marking_teacher, teacher_group
):
    """The grid saves a column at a time, so the whole batch must be validated before any
    of it is written. Row 1 here is a perfectly valid mark; row 2's excuse is invalid
    (excused=True with a blank note). The request must 422, and row 1 must never have
    been written — not even to the session — proving validation is a pre-pass over the
    whole batch, not a per-row check interleaved with writes."""
    from src.schemas.models import UserInDB

    event_id, first_user_id = event_and_student
    second_student = UserInDB(
        name="Тест Студентов 2", email=f"exc-2-{datetime.now().timestamp()}@test.local",
        hashed_password="x", role="student",
    )
    db.add(second_student)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        update_attendance_bulk(
            BulkAttendanceInputSchema(
                updates=[
                    AttendanceInputSchema(
                        group_id=teacher_group.id, week_number=1, lesson_index=1,
                        student_id=first_user_id, score=1, status="attended",
                        event_id=event_id,
                    ),
                    AttendanceInputSchema(
                        group_id=teacher_group.id, week_number=1, lesson_index=2,
                        student_id=second_student.id, score=0, status="missed",
                        event_id=event_id, excused=True, excuse_note="  ",
                    ),
                ]
            ),
            marking_teacher,
            db,
        )
    assert exc.value.status_code == 422

    row = AttendanceService.get_by_event_and_user(db, event_id, first_user_id)
    assert row is None


import asyncio

from src.gamification.routes.leaderboard import get_weekly_lessons_with_hw_status


def test_the_grid_response_carries_the_excuse(db, event_and_student, marking_teacher, teacher_group):
    """Ячейка обязана приехать на клиент с причиной: иначе учитель, открыв сетку заново,
    увидит просто «Не был» и потеряет то, что сам написал.

    `get_group_leaderboard` (the other, curator-only leaderboard endpoint in this module)
    returns a flat per-student row with no `lessons` map and doesn't accept a `teacher`
    caller at all. The grid a teacher actually reopens — with `students[].lessons{}` cells
    keyed by lesson number — is `get_weekly_lessons_with_hw_status`; see
    tests/test_weekly_lessons_teacher_access.py for the same call shape. It's async, so it
    is driven with asyncio.run like every other direct call to it in this suite.

    Two rows the existing fixtures don't provide are added here: an `EventGroup` linking
    the event to `teacher_group` (this endpoint finds events by joining EventGroup, not by
    event.teacher_id), and a `GroupStudent` membership row (student_ids come from
    GroupStudent; without one the function returns `students: []` before ever reaching the
    per-lesson loop).
    """
    from src.courses.models import GroupStudent
    from src.schemas.models import EventGroup

    event_id, user_id = event_and_student
    db.add(EventGroup(event_id=event_id, group_id=teacher_group.id))
    db.add(GroupStudent(group_id=teacher_group.id, student_id=user_id))
    db.flush()

    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="соревнования",
    )
    db.flush()

    payload = asyncio.run(get_weekly_lessons_with_hw_status(
        teacher_group.id, week_number=1, current_user=marking_teacher, db=db,
    ))

    cells = [
        cell
        for row in payload["students"]
        for cell in row["lessons"].values()
        if cell["event_id"] == event_id
    ]
    assert cells, "урок не попал в сетку — проверьте week_number фикстуры"
    assert cells[0]["excused"] is True
    assert cells[0]["excuse_note"] == "соревнования"


def test_the_attendance_matrix_carries_the_excuse(db, event_and_student, marking_teacher, teacher_group):
    """`/curator/full-attendance/{group_id}` (`get_group_full_attendance_matrix`) is the
    matrix the mobile teacher attendance screen reopens — `useAttendanceEditor.ts` reads its
    per-cell `attendance_status` to seed the editor. It builds its own cell dict from the
    same `AttendanceService.get_attendance_map_for_events` map the grid uses, but — unlike
    the grid — never carried `excused`/`excuse_note` onto that cell, so a teacher's reason
    was silently dropped the moment this screen reopened.

    The function is plain `def`, not async, so it's called directly, no asyncio.run needed.
    Like the grid, it authorises a teacher caller on `group.teacher_id == current_user.id`,
    which `teacher_group`/`marking_teacher` already satisfy. It also needs the same two rows
    the grid test added by hand: an `EventGroup` linking the event to the group (events are
    found by joining EventGroup/EventCourse, not by event.teacher_id) and a `GroupStudent`
    membership row (student_ids come from GroupStudent).

    A second student with no attendance row at all is added to prove the *other* half of
    the invariant: an unmarked cell must default to `excused: False, excuse_note: None`
    rather than leaving the keys off or defaulting to None/None — the grid's own test never
    exercised this default, so it stays unverified twice if skipped here too.
    """
    from src.courses.models import GroupStudent
    from src.schemas.models import EventGroup, UserInDB
    from src.gamification.routes.leaderboard import get_group_full_attendance_matrix

    event_id, excused_user_id = event_and_student
    db.add(EventGroup(event_id=event_id, group_id=teacher_group.id))
    db.add(GroupStudent(group_id=teacher_group.id, student_id=excused_user_id))

    unmarked_student = UserInDB(
        name="Тест Немаркированный", email=f"exc-um-{datetime.now().timestamp()}@test.local",
        hashed_password="x", role="student",
    )
    db.add(unmarked_student)
    db.flush()
    db.add(GroupStudent(group_id=teacher_group.id, student_id=unmarked_student.id))
    db.flush()

    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=excused_user_id, status="absent",
        excused=True, excuse_note="соревнования",
    )
    db.flush()

    payload = get_group_full_attendance_matrix(
        teacher_group.id, current_user=marking_teacher, db=db,
    )

    rows_by_student = {row["student_id"]: row for row in payload["students"]}

    excused_cells = [
        cell for cell in rows_by_student[excused_user_id]["lessons"].values()
        if cell["event_id"] == event_id
    ]
    assert excused_cells, "урок не попал в матрицу"
    assert excused_cells[0]["excused"] is True
    assert excused_cells[0]["excuse_note"] == "соревнования"

    unmarked_cells = [
        cell for cell in rows_by_student[unmarked_student.id]["lessons"].values()
        if cell["event_id"] == event_id
    ]
    assert unmarked_cells, "урок не попал в матрицу"
    assert unmarked_cells[0]["excused"] is False
    assert unmarked_cells[0]["excuse_note"] is None


def test_report_counts_excused_absences_separately(db, event_and_student, teacher_group):
    """«absent» остаётся полным числом пропусков: отчёт добавляет разрез, а не меняет
    существующую цифру — иначе у всех, кто её читает, молча изменится смысл колонки."""
    from src.courses.models import GroupStudent
    from src.reports.services import _attendance_section
    from src.schemas.models import EventGroup

    event_id, user_id = event_and_student
    db.add(EventGroup(event_id=event_id, group_id=teacher_group.id))
    db.add(GroupStudent(group_id=teacher_group.id, student_id=user_id))
    db.flush()

    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="болел",
    )
    db.flush()
    section = _attendance_section(db, user_id)
    assert section["absent"] == 1
    assert section["absent_excused"] == 1
    assert section["absences"][0]["excused"] is True
    assert section["absences"][0]["excuse_note"] == "болел"


# --- сохранение, которое про уважительность не знает ---------------------------------------
#
# Оба пишущих контракта объявляют ``excused`` тристабильным (``None`` — «не сообщаю»).
# Дефолт ``False`` здесь означал бы, что любой клиент, который поле не шлёт, снимает
# уважительную: панель замен пересылает весь список учеников при каждом сохранении, а
# мобильная очередь вслепую переотправляет payload, записанный до фичи. Один отмеченный
# ученик стирал бы причины у всего урока.


def test_event_endpoint_preserves_an_excuse_it_was_not_told_about(
    db, event_and_student, marking_teacher
):
    event_id, user_id = event_and_student
    update_event_attendance(
        event_id,
        AttendanceBulkUpdateSchema(
            attendance=[
                AttendanceRecord(
                    student_id=user_id, status="missed",
                    excused=True, excuse_note="был на олимпиаде",
                )
            ]
        ),
        db,
        marking_teacher,
    )

    # Ровно то, что шлёт панель замен: тот же ученик, тот же статус, ни слова про причину.
    update_event_attendance(
        event_id,
        AttendanceBulkUpdateSchema(
            attendance=[AttendanceRecord(student_id=user_id, status="missed")]
        ),
        db,
        marking_teacher,
    )

    row = AttendanceService.get_by_event_and_user(db, event_id, user_id)
    assert row.excused is True
    assert row.excuse_note == "был на олимпиаде"


def test_grid_bulk_preserves_an_excuse_it_was_not_told_about(
    db, event_and_student, marking_teacher, teacher_group
):
    event_id, user_id = event_and_student
    update_attendance_bulk(
        BulkAttendanceInputSchema(
            updates=[
                AttendanceInputSchema(
                    group_id=teacher_group.id, week_number=1, lesson_index=1,
                    student_id=user_id, score=0, status="missed", event_id=event_id,
                    excused=True, excuse_note="болел",
                )
            ]
        ),
        marking_teacher,
        db,
    )

    update_attendance_bulk(
        BulkAttendanceInputSchema(
            updates=[
                AttendanceInputSchema(
                    group_id=teacher_group.id, week_number=1, lesson_index=1,
                    student_id=user_id, score=0, status="missed", event_id=event_id,
                )
            ]
        ),
        marking_teacher,
        db,
    )

    row = AttendanceService.get_by_event_and_user(db, event_id, user_id)
    assert row.excused is True
    assert row.excuse_note == "болел"


def test_an_explicit_false_still_lifts_the_excuse(
    db, event_and_student, marking_teacher, teacher_group
):
    """Тристабильность не должна превратиться в «снять нельзя»: ``False`` — по-прежнему
    явное решение клиента, и оно обязано сработать."""
    event_id, user_id = event_and_student
    update_attendance_bulk(
        BulkAttendanceInputSchema(
            updates=[
                AttendanceInputSchema(
                    group_id=teacher_group.id, week_number=1, lesson_index=1,
                    student_id=user_id, score=0, status="missed", event_id=event_id,
                    excused=True, excuse_note="болел",
                )
            ]
        ),
        marking_teacher,
        db,
    )
    update_attendance_bulk(
        BulkAttendanceInputSchema(
            updates=[
                AttendanceInputSchema(
                    group_id=teacher_group.id, week_number=1, lesson_index=1,
                    student_id=user_id, score=0, status="missed", event_id=event_id,
                    excused=False,
                )
            ]
        ),
        marking_teacher,
        db,
    )
    row = AttendanceService.get_by_event_and_user(db, event_id, user_id)
    assert row.excused is False
    assert row.excuse_note is None
    assert row.excused_by_user_id is None
    assert row.excused_at is None


# --- карточка урока: чтение --------------------------------------------------------------


def test_the_lesson_card_reads_back_the_excuse(db, event_and_student, marking_teacher, teacher_group):
    """`GET /events/{id}/participants` — чтение того самого экрана, на котором учитель
    уважительную и ставит. Поле объявлено в ``response_model``, поэтому неперенесённое
    здесь не «просочится» само: учитель, переоткрыв карточку, увидел бы обычный «Не был».
    """
    from src.courses.models import GroupStudent
    from src.events.routes.events import get_event_participants
    from src.schemas.models import EventGroup

    event_id, user_id = event_and_student
    db.add(EventGroup(event_id=event_id, group_id=teacher_group.id))
    db.add(GroupStudent(group_id=teacher_group.id, student_id=user_id))
    db.flush()

    update_event_attendance(
        event_id,
        AttendanceBulkUpdateSchema(
            attendance=[
                AttendanceRecord(
                    student_id=user_id, status="missed",
                    excused=True, excuse_note="семейные обстоятельства",
                )
            ]
        ),
        db,
        marking_teacher,
    )

    rows = get_event_participants(event_id, None, db, marking_teacher)
    mine = [r for r in rows if r.student_id == user_id]
    assert mine, "ученик не попал в карточку урока"
    assert mine[0].attendance_status == "missed"
    assert mine[0].excused is True
    assert mine[0].excuse_note == "семейные обстоятельства"


def test_the_lesson_card_defaults_an_unmarked_student_to_unexcused(
    db, event_and_student, marking_teacher, teacher_group
):
    from src.courses.models import GroupStudent
    from src.events.routes.events import get_event_participants
    from src.schemas.models import EventGroup

    event_id, user_id = event_and_student
    db.add(EventGroup(event_id=event_id, group_id=teacher_group.id))
    db.add(GroupStudent(group_id=teacher_group.id, student_id=user_id))
    db.flush()

    rows = get_event_participants(event_id, None, db, marking_teacher)
    mine = [r for r in rows if r.student_id == user_id]
    assert mine[0].excused is False
    assert mine[0].excuse_note is None


# --- одиночная ячейка ---------------------------------------------------------------------


def test_single_cell_endpoint_stores_the_excuse(
    db, event_and_student, marking_teacher, teacher_group
):
    """`POST /leaderboard/curator/attendance` делит схему с пакетным роутом, поэтому
    уважительность объявлена и в его контракте. Роут, который поле принимает, отвечает
    200 и ничего не пишет, — худший из возможных: клиент считает причину сохранённой."""
    from src.gamification.routes.leaderboard import update_attendance

    event_id, user_id = event_and_student
    update_attendance(
        AttendanceInputSchema(
            group_id=teacher_group.id, week_number=1, lesson_index=1,
            student_id=user_id, score=0, status="missed", event_id=event_id,
            excused=True, excuse_note="олимпиада",
        ),
        marking_teacher,
        db,
    )
    row = AttendanceService.get_by_event_and_user(db, event_id, user_id)
    assert row.excused is True
    assert row.excuse_note == "олимпиада"
    assert row.excused_by_user_id == marking_teacher.id


def test_single_cell_endpoint_refuses_an_excuse_without_a_note(
    db, event_and_student, marking_teacher, teacher_group
):
    from src.gamification.routes.leaderboard import update_attendance

    event_id, user_id = event_and_student
    with pytest.raises(HTTPException) as exc:
        update_attendance(
            AttendanceInputSchema(
                group_id=teacher_group.id, week_number=1, lesson_index=1,
                student_id=user_id, score=0, status="missed", event_id=event_id,
                excused=True, excuse_note="   ",
            ),
            marking_teacher,
            db,
        )
    assert exc.value.status_code == 422
    # Тексты 422 обязаны совпадать байт в байт со всеми остальными местами.
    assert exc.value.detail == "Уважительный пропуск требует причину"


def test_single_cell_endpoint_refuses_an_excused_present(
    db, event_and_student, marking_teacher, teacher_group
):
    from src.gamification.routes.leaderboard import update_attendance

    event_id, user_id = event_and_student
    with pytest.raises(HTTPException) as exc:
        update_attendance(
            AttendanceInputSchema(
                group_id=teacher_group.id, week_number=1, lesson_index=1,
                student_id=user_id, score=1, status="attended", event_id=event_id,
                excused=True, excuse_note="болел",
            ),
            marking_teacher,
            db,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == "Уважительной может быть только отметка о пропуске"


# --- авторство уважительной ----------------------------------------------------------------


@pytest.fixture()
def two_excusers(db):
    """Два настоящих пользователя: ``excused_by_user_id`` — FK на ``users``."""
    from src.schemas.models import UserInDB

    stamp = datetime.now().timestamp()
    first = UserInDB(
        name="Первый Отпустивший", email=f"exc-a-{stamp}@test.local",
        hashed_password="x", role="teacher",
    )
    second = UserInDB(
        name="Второй Отпустивший", email=f"exc-b-{stamp}@test.local",
        hashed_password="x", role="teacher",
    )
    db.add_all([first, second])
    db.flush()
    return first.id, second.id


def test_attribution_survives_an_edit_of_a_neighbouring_cell(db, event_and_student, two_excusers):
    """Сетка сохраняет колонку целиком: нетронутая уважительная строка приезжает в
    ``upsert_for_event`` при каждой правке соседней ячейки. Безусловная простановка
    отдавала бы «кто и когда отпустил» последнему, кто открыл экран."""
    first_id, second_id = two_excusers
    event_id, user_id = event_and_student
    first = AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="болел", excused_by_user_id=first_id,
    )
    original_by, original_at = first.excused_by_user_id, first.excused_at
    assert original_by == first_id and original_at is not None

    again = AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="болел", excused_by_user_id=second_id,
    )
    assert again.excused_by_user_id == original_by
    assert again.excused_at == original_at


def test_a_changed_note_restamps_the_attribution(db, event_and_student, two_excusers):
    """Правка причины — новое решение об уважительности, и его автор новый."""
    first_id, second_id = two_excusers
    event_id, user_id = event_and_student
    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="болел", excused_by_user_id=first_id,
    )
    row = AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="был на олимпиаде", excused_by_user_id=second_id,
    )
    assert row.excused_by_user_id == second_id
    assert row.excuse_note == "был на олимпиаде"


def test_re_excusing_a_lifted_row_stamps_the_new_author(db, event_and_student, two_excusers):
    first_id, second_id = two_excusers
    event_id, user_id = event_and_student
    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="болел", excused_by_user_id=first_id,
    )
    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent", excused=False,
    )
    row = AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="болел", excused_by_user_id=second_id,
    )
    assert row.excused_by_user_id == second_id
    assert row.excused_at is not None


# --- защита чтения от несогласованной строки ----------------------------------------------
#
# Инвариант «уважительная только на пропуске» держится валидацией на записи в LMS, но в эту
# таблицу пишет ещё и CRM: её ``upsert_attendance`` переставляет absent → present, не трогая
# колонку ``excused``, а CHECK в БД сторожит только «есть причина». Значит строка
# excused=true при статусе present в природе возможна, и читатель обязан считать её обычным
# присутствием, а не рисовать янтарную «Ув.» на зелёной ячейке.


@pytest.fixture()
def inconsistent_row(db, event_and_student, teacher_group):
    """Строка, какую может оставить после себя CRM: present + excused."""
    from src.courses.models import GroupStudent
    from src.schemas.models import EventGroup

    event_id, user_id = event_and_student
    db.add(EventGroup(event_id=event_id, group_id=teacher_group.id))
    db.add(GroupStudent(group_id=teacher_group.id, student_id=user_id))
    db.flush()

    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="absent",
        excused=True, excuse_note="болел",
    )
    db.flush()
    # Ровно то, что делает CRM: статус переставлен, колонка не тронута. Через
    # upsert_for_event так не получится — он инвариант и чинит.
    db.execute(
        text(
            "UPDATE attendances SET status = 'present' "
            "WHERE event_id = :e AND user_id = :u"
        ),
        {"e": event_id, "u": user_id},
    )
    db.flush()
    db.expire_all()
    return event_id, user_id


def test_the_grid_does_not_render_an_excuse_on_a_present_cell(
    db, inconsistent_row, marking_teacher, teacher_group
):
    event_id, user_id = inconsistent_row
    payload = asyncio.run(get_weekly_lessons_with_hw_status(
        teacher_group.id, week_number=1, current_user=marking_teacher, db=db,
    ))
    cells = [
        cell
        for row in payload["students"]
        for cell in row["lessons"].values()
        if cell["event_id"] == event_id
    ]
    assert cells, "урок не попал в сетку"
    assert cells[0]["attendance_status"] == "attended"
    assert cells[0]["excused"] is False
    assert cells[0]["excuse_note"] is None


def test_the_matrix_does_not_render_an_excuse_on_a_present_cell(
    db, inconsistent_row, marking_teacher, teacher_group
):
    from src.gamification.routes.leaderboard import get_group_full_attendance_matrix

    event_id, user_id = inconsistent_row
    payload = get_group_full_attendance_matrix(
        teacher_group.id, current_user=marking_teacher, db=db,
    )
    rows_by_student = {row["student_id"]: row for row in payload["students"]}
    cells = [
        cell for cell in rows_by_student[user_id]["lessons"].values()
        if cell["event_id"] == event_id
    ]
    assert cells, "урок не попал в матрицу"
    assert cells[0]["attendance_status"] == "attended"
    assert cells[0]["excused"] is False
    assert cells[0]["excuse_note"] is None


def test_the_lesson_card_does_not_render_an_excuse_on_a_present_cell(
    db, inconsistent_row, marking_teacher
):
    from src.events.routes.events import get_event_participants

    event_id, user_id = inconsistent_row
    rows = get_event_participants(event_id, None, db, marking_teacher)
    mine = [r for r in rows if r.student_id == user_id]
    assert mine[0].attendance_status == "attended"
    assert mine[0].excused is False
    assert mine[0].excuse_note is None


# --- отчёт ---------------------------------------------------------------------------------


def test_only_absences_carry_the_excuse_fields(db, event_and_student, teacher_group):
    """На опоздании эти ключи были бы всегда False/None — поле, которое ничего не значит,
    но которое читатель отчёта рано или поздно попробует прочитать."""
    from src.courses.models import GroupStudent
    from src.reports.services import _attendance_section
    from src.schemas.models import EventGroup

    event_id, user_id = event_and_student
    db.add(EventGroup(event_id=event_id, group_id=teacher_group.id))
    db.add(GroupStudent(group_id=teacher_group.id, student_id=user_id))
    db.flush()

    AttendanceService.upsert_for_event(
        db, event_id=event_id, user_id=user_id, status="late", score=1,
    )
    db.flush()

    section = _attendance_section(db, user_id)
    assert section["lates"], "опоздание не попало в отчёт"
    assert set(section["lates"][0]) == {"date", "title"}
