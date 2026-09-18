"""Каскад выбора шаблона. Вход — голый словарь WeekFacts, база не нужна."""
import pytest

from src.reports.parent.template import PROSE_SLOTS, pick_template


def facts(**over) -> dict:
    """Спокойная неделя без единого повода для тревоги: дефолт — t1."""
    base = {
        "student": {"id": 1, "name": "Амир"},
        "group": {"id": 10, "name": "SAT-3"},
        "week": {"start": "2026-09-14", "end": "2026-09-20"},
        "test": {
            "program": "sat", "label": "Week 5", "date": "2026-09-19",
            "verbal": {"correct": 17, "total": 27},
            "math": {"correct": 15, "total": 22},
            "prev": {"label": "Week 4",
                     "verbal": {"correct": 14, "total": 27},
                     "math": {"correct": 15, "total": 22}},
            "delta": {"verbal": 3, "math": 0},
        },
        "test_unavailable": False,
        "homework": {"assigned": 3, "submitted": 3, "missing": []},
        "attendance": {"lessons": 3, "present": 3, "late": 0, "absences": []},
        "talk": None,
        "strength": None,
        "weakness": None,
        "teacher_feedback": None,
        "no_growth_streak": 0,
        "curator_note": None,
    }
    base.update(over)
    return base


def test_calm_week_falls_through_to_t1():
    key, reason = pick_template(facts())
    assert key == "t1"
    assert reason


def test_unexcused_absence_triggers_t5():
    key, reason = pick_template(facts(attendance={
        "lessons": 3, "present": 2, "late": 0,
        "absences": [{"date": "2026-09-16", "excused": False}],
    }))
    assert key == "t5"
    assert "16.09" in reason


def test_excused_absence_alone_does_not_trigger_t5():
    key, _ = pick_template(facts(attendance={
        "lessons": 3, "present": 2, "late": 0,
        "absences": [{"date": "2026-09-16", "excused": True}],
    }))
    assert key == "t1"


def test_less_than_half_homework_triggers_t5():
    key, reason = pick_template(facts(homework={
        "assigned": 3, "submitted": 1, "missing": ["Unit 5 Reading"],
    }))
    assert key == "t5"
    assert "1 из 3" in reason


def test_exactly_half_homework_does_not_trigger_t5():
    key, _ = pick_template(facts(homework={"assigned": 4, "submitted": 2, "missing": []}))
    assert key == "t1"


def test_single_unsubmitted_assignment_does_not_trigger_t5():
    # Порог требует минимум двух заданных: одно несданное — ещё не система.
    key, _ = pick_template(facts(homework={"assigned": 1, "submitted": 0, "missing": ["Unit 5"]}))
    assert key == "t1"


def test_two_tests_without_growth_trigger_t5():
    key, reason = pick_template(facts(no_growth_streak=2))
    assert key == "t5"
    assert "рост" in reason.lower()


def test_one_test_without_growth_does_not_trigger_t5():
    key, _ = pick_template(facts(no_growth_streak=1))
    assert key == "t1"


def test_no_test_but_talk_data_gives_t2():
    key, reason = pick_template(facts(
        test=None,
        talk={"lessons": 3, "lessons_spoke": 2, "avg_seconds": 75,
              "questions": 4, "answers": 2},
    ))
    assert key == "t2"
    assert "тест" in reason.lower()


def test_platform_outage_never_claims_there_was_no_test():
    # test_unavailable — это «мы не знаем», а не «теста не было». Утверждать второе
    # родителю нельзя, поэтому каскад обязан пройти мимо t2.
    key, reason = pick_template(facts(
        test=None,
        test_unavailable=True,
        talk={"lessons": 3, "lessons_spoke": 2, "avg_seconds": 75,
              "questions": 4, "answers": 2},
    ))
    assert key == "t1"
    assert "тест" not in reason.lower()


def test_problems_still_win_during_a_platform_outage():
    key, _ = pick_template(facts(
        test=None,
        test_unavailable=True,
        attendance={"lessons": 3, "present": 2, "late": 0,
                    "absences": [{"date": "2026-09-16", "excused": False}]},
    ))
    assert key == "t5"


def test_no_test_and_no_talk_falls_to_t1():
    key, _ = pick_template(facts(test=None, talk=None))
    assert key == "t1"


def test_no_test_with_empty_talk_window_falls_to_t1():
    key, _ = pick_template(facts(test=None, talk={
        "lessons": 0, "lessons_spoke": 0, "avg_seconds": 0,
        "questions": None, "answers": None,
    }))
    assert key == "t1"


def test_weakness_with_a_test_gives_t4():
    key, reason = pick_template(facts(weakness={
        "label": "Reading", "source": "quiz", "pct": 55,
    }))
    assert key == "t4"
    assert "Reading" in reason


def test_problems_outrank_weakness():
    # Каскад читается сверху вниз: пропуск важнее слабой зоны.
    key, _ = pick_template(facts(
        weakness={"label": "Reading", "source": "quiz", "pct": 55},
        attendance={"lessons": 3, "present": 2, "late": 0,
                    "absences": [{"date": "2026-09-16", "excused": False}]},
    ))
    assert key == "t5"


def test_missing_homework_section_never_triggers_t5():
    # Учитель не задавал ДЗ всю неделю — это не повод для тревожного шаблона.
    key, _ = pick_template(facts(homework=None))
    assert key == "t1"


@pytest.mark.parametrize("key", ["t1", "t2", "t3", "t4", "t5"])
def test_every_template_declares_its_slots(key):
    assert key in PROSE_SLOTS
    assert isinstance(PROSE_SLOTS[key], tuple)
