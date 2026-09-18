"""Сбор WeekFacts: недельное окно, синонимы статусов, кандидаты в сильную/слабую.

Часть тестов требует Postgres (фикстура db делает skip, если базы нет), часть — чистая.
Внешние платформы замоканы всегда.
"""
from datetime import date, datetime, timedelta

import pytest

from src.reports.parent.facts import (
    build_week_facts,
    pick_candidates,
    no_growth_streak,
    _select_test,
    _nuet_week_label,
)


# ---------------------------------------------------------------- чистые тесты

def test_no_growth_streak_counts_consecutive_flat_tests():
    history = [
        {"verbal": {"correct": 10}, "math": {"correct": 10}},
        {"verbal": {"correct": 10}, "math": {"correct": 10}},
        {"verbal": {"correct": 10}, "math": {"correct": 10}},
    ]
    assert no_growth_streak(history) == 2


def test_growth_in_one_section_resets_the_streak():
    history = [
        {"verbal": {"correct": 10}, "math": {"correct": 10}},
        {"verbal": {"correct": 13}, "math": {"correct": 10}},
    ]
    assert no_growth_streak(history) == 0


def test_single_test_has_no_streak():
    assert no_growth_streak([{"verbal": {"correct": 10}, "math": {"correct": 10}}]) == 0


def test_quiz_above_threshold_becomes_a_strength():
    strength, weakness = pick_candidates(
        quizzes=[{"lesson_title": "Linear Equations", "average_pct": 92.0}],
        test=None,
    )
    assert strength == {"label": "Linear Equations", "source": "quiz", "pct": 92}
    assert weakness is None


def test_quiz_below_threshold_becomes_a_weakness():
    _, weakness = pick_candidates(
        quizzes=[{"lesson_title": "Reading Comprehension", "average_pct": 55.0}],
        test=None,
    )
    assert weakness == {"label": "Reading Comprehension", "source": "quiz", "pct": 55}


def test_midrange_quiz_produces_nothing():
    # 70% — не повод ни хвалить, ни тревожиться. Строки в отчёте не будет.
    strength, weakness = pick_candidates(
        quizzes=[{"lesson_title": "Geometry", "average_pct": 70.0}],
        test=None,
    )
    assert strength is None and weakness is None


def test_section_gap_becomes_a_weakness_when_quizzes_are_silent():
    _, weakness = pick_candidates(
        quizzes=[],
        test={"verbal": {"correct": 10, "total": 27}, "math": {"correct": 20, "total": 22}},
    )
    assert weakness["label"] == "Verbal"
    assert weakness["source"] == "section_gap"


def test_small_section_gap_is_not_a_weakness():
    _, weakness = pick_candidates(
        quizzes=[],
        test={"verbal": {"correct": 18, "total": 27}, "math": {"correct": 15, "total": 22}},
    )
    assert weakness is None


def test_quiz_outranks_section_gap():
    _, weakness = pick_candidates(
        quizzes=[{"lesson_title": "Reading Comprehension", "average_pct": 40.0}],
        test={"verbal": {"correct": 10, "total": 27}, "math": {"correct": 20, "total": 22}},
    )
    assert weakness["source"] == "quiz"


class _Group:
    """Достаточно группы, чтобы посчитать номер недели курса."""

    def __init__(self, created_at, name="NUET-1", program_type="nuet", offset=0):
        self.created_at = created_at
        self.name = name
        self.program_type = program_type
        self.weekly_set_week_offset = offset


def _sat_week(day, verbal, math):
    return {"week_label": day, "completed_at": f"{day}T10:00:00Z",
            "verbal": {"correct": verbal, "total": 27},
            "math": {"correct": math, "total": 22}}


def _nuet_week(label, verbal, math):
    return {"week_label": label,
            "verbal": {"correct": verbal, "total": 27},
            "math": {"correct": math, "total": 22}}


def test_sat_test_is_matched_by_date_and_prev_is_the_one_before():
    weekly = {"sat": [_sat_week("2026-09-07", 12, 14), _sat_week("2026-09-16", 17, 15)]}
    test, history, _ = _select_test(weekly, date(2026, 9, 14), date(2026, 9, 20))
    assert test["verbal"]["correct"] == 17
    assert test["prev"]["verbal"]["correct"] == 12
    assert test["delta"]["verbal"] == 5


def test_sat_week_without_a_test_yields_nothing_and_an_empty_history():
    weekly = {"sat": [_sat_week("2026-09-07", 12, 14)]}
    test, history, _ = _select_test(weekly, date(2026, 9, 14), date(2026, 9, 20))
    assert test is None
    assert history == []


def test_nuet_picks_the_week_that_was_asked_for_not_the_latest():
    # Именно тот случай, ради которого фикс: отчёт за прошлую неделю обязан показать
    # прошлую неделю, а не последнюю запись, которую вернула платформа.
    group = _Group(datetime(2026, 8, 31))          # Week 1 = 31.08..06.09
    weekly = {"nuet": [_nuet_week("Week 1", 10, 11), _nuet_week("Week 2", 13, 12),
                       _nuet_week("Week 3", 18, 16)]}
    label = _nuet_week_label(group, date(2026, 9, 7))   # это Week 2
    assert label == "Week 2"
    test, _, _ = _select_test(weekly, date(2026, 9, 7), date(2026, 9, 13), label)
    assert test["label"] == "Week 2"
    assert test["verbal"]["correct"] == 13
    assert test["prev"]["label"] == "Week 1"


def test_nuet_week_the_student_skipped_yields_no_test():
    group = _Group(datetime(2026, 8, 31))
    weekly = {"nuet": [_nuet_week("Week 1", 10, 11), _nuet_week("Week 3", 18, 16)]}
    label = _nuet_week_label(group, date(2026, 9, 7))   # Week 2, которой нет в истории
    test, history, _ = _select_test(weekly, date(2026, 9, 7), date(2026, 9, 13), label)
    assert test is None
    assert history == []


def test_nuet_week_offset_shifts_the_numbering():
    # Группа стартовала в середине недели: offset=1 сдвигает нумерацию на неделю назад.
    group = _Group(datetime(2026, 8, 31), offset=1)
    assert _nuet_week_label(group, date(2026, 9, 7)) == "Week 1"


def test_nuet_without_a_group_has_no_label():
    assert _nuet_week_label(None, date(2026, 9, 7)) is None


# ------------------------------------------------------------- тесты с Postgres

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


@pytest.fixture
def student_in_group(db):
    from src.schemas.models import Group, GroupStudent, UserInDB
    student = UserInDB(name="Амир", email="amir-parent-report@test.kz",
                       role="student", hashed_password="x")
    db.add(student)
    db.flush()
    group = Group(name="SAT-3", program_type="sat")
    db.add(group)
    db.flush()
    db.add(GroupStudent(group_id=group.id, student_id=student.id))
    db.flush()
    return student, group


def _class_event(db, group_id: int, when: datetime, title: str = "Урок", created_by: int = None):
    # end_datetime и created_by NOT NULL в БД (см. src/events/models.py) — брифовский
    # фикстурный хелпер их не выставлял и падал на реальном Postgres NotNullViolation.
    from src.schemas.models import Event, EventGroup
    event = Event(title=title, event_type="class", start_datetime=when,
                  end_datetime=when + timedelta(hours=1), created_by=created_by, is_active=True)
    db.add(event)
    db.flush()
    db.add(EventGroup(event_id=event.id, group_id=group_id))
    db.flush()
    return event


@pytest.fixture
def no_external(monkeypatch):
    """Ни один тест не ходит на платформы тестов."""
    async def _empty(db, student):
        return {"sat": [], "ielts": [], "nuet": [], "errors": []}
    monkeypatch.setattr("src.reports.parent.facts.fetch_weekly_tests", _empty)


@pytest.mark.asyncio
async def test_attendance_counts_only_events_inside_the_window(db, student_in_group, no_external):
    from src.schemas.models import Attendance
    student, group = student_in_group
    inside = _class_event(db, group.id, datetime(2026, 9, 16, 5, 0), created_by=student.id)   # ср, в окне
    outside = _class_event(db, group.id, datetime(2026, 9, 9, 5, 0), created_by=student.id)   # неделей раньше
    db.add(Attendance(user_id=student.id, event_id=inside.id, status="present"))
    db.add(Attendance(user_id=student.id, event_id=outside.id, status="absent"))
    db.flush()

    facts = await build_week_facts(db, student.id, date(2026, 9, 14))
    assert facts["attendance"]["lessons"] == 1
    assert facts["attendance"]["absences"] == []


@pytest.mark.asyncio
async def test_missed_synonym_counts_as_an_absence(db, student_in_group, no_external):
    from src.schemas.models import Attendance
    student, group = student_in_group
    event = _class_event(db, group.id, datetime(2026, 9, 16, 5, 0), created_by=student.id)
    db.add(Attendance(user_id=student.id, event_id=event.id, status="missed"))
    db.flush()

    facts = await build_week_facts(db, student.id, date(2026, 9, 14))
    assert facts["attendance"]["absences"] == [{"date": "2026-09-16", "excused": False}]


@pytest.mark.asyncio
async def test_registered_but_unmarked_lesson_is_not_counted(db, student_in_group, no_external):
    from src.schemas.models import Attendance
    student, group = student_in_group
    event = _class_event(db, group.id, datetime(2026, 9, 16, 5, 0), created_by=student.id)
    db.add(Attendance(user_id=student.id, event_id=event.id, status="registered"))
    db.flush()

    facts = await build_week_facts(db, student.id, date(2026, 9, 14))
    # Урок, на котором никого не отметили, — не проведённое занятие. Иначе отчёт скажет
    # «занятий 1» и при этом ни присутствий, ни пропусков.
    assert facts["attendance"]["lessons"] == 0


@pytest.mark.asyncio
async def test_homework_section_is_none_when_nothing_was_due(db, student_in_group, no_external):
    student, _ = student_in_group
    facts = await build_week_facts(db, student.id, date(2026, 9, 14))
    assert facts["homework"] is None


@pytest.mark.asyncio
async def test_platform_error_sets_test_unavailable(db, student_in_group, monkeypatch):
    student, _ = student_in_group

    async def _broken(db_, student_):
        return {"sat": [], "ielts": [], "nuet": [], "errors": ["sat: timeout"]}
    monkeypatch.setattr("src.reports.parent.facts.fetch_weekly_tests", _broken)

    facts = await build_week_facts(db, student.id, date(2026, 9, 14))
    assert facts["test"] is None
    assert facts["test_unavailable"] is True


@pytest.mark.asyncio
async def test_curator_note_lands_in_facts(db, student_in_group, no_external):
    student, _ = student_in_group
    facts = await build_week_facts(db, student.id, date(2026, 9, 14),
                                   curator_note="Устал, болел в начале недели")
    assert facts["curator_note"] == "Устал, болел в начале недели"
