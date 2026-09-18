"""Рендер сообщения. Все числа проставляет код — проверяем, что именно так и есть."""
from src.reports.parent.template import render


def facts(**over) -> dict:
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
        "homework": {"assigned": 3, "submitted": 2, "missing": ["Unit 5 Reading"]},
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


def test_t1_renders_scores_and_previous_week():
    out = render(facts(), "t1", {"progress": "Есть рост по Verbal на 3 балла.",
                                 "recommendation": "Читать статью в день.",
                                 "forecast": "Темп хороший."})
    assert "Verbal: 17/27 (прошлая неделя: 14/27)" in out
    assert "Math: 15/22 (прошлая неделя: 15/22)" in out
    assert "Есть рост по Verbal на 3 балла." in out
    assert out.startswith("Здравствуйте! 🤍")


def test_homework_line_uses_real_counts():
    out = render(facts(), "t1", {"progress": "x", "recommendation": "y", "forecast": "z"})
    assert "Выполнено 2 из 3" in out


def test_homework_section_disappears_when_nothing_was_assigned():
    out = render(facts(homework=None), "t1",
                 {"progress": "x", "recommendation": "y", "forecast": "z"})
    assert "ДЗ" not in out


def test_test_section_disappears_when_no_test_in_window():
    out = render(facts(test=None), "t1",
                 {"progress": "x", "recommendation": "y", "forecast": "z"})
    assert "Verbal" not in out
    assert "Math" not in out


def test_previous_week_omitted_when_there_is_no_history():
    payload = facts()
    payload["test"] = {**payload["test"], "prev": None, "delta": None}
    out = render(payload, "t1", {"progress": "x", "recommendation": "y", "forecast": "z"})
    assert "Verbal: 17/27" in out
    assert "прошлая неделя" not in out


def test_attendance_line_names_absence_dates():
    out = render(facts(attendance={
        "lessons": 3, "present": 2, "late": 0,
        "absences": [{"date": "2026-09-16", "excused": False}],
    }), "t1", {"progress": "x", "recommendation": "y", "forecast": "z"})
    assert "Пропуск (16.09)" in out


def test_clean_attendance_says_all_lessons():
    out = render(facts(), "t1", {"progress": "x", "recommendation": "y", "forecast": "z"})
    assert "Все занятия" in out


def test_missing_prose_slot_drops_its_line_entirely():
    # Слота strength в prose нет — строки «Что получается хорошо» быть не должно.
    out = render(facts(), "t1", {"progress": "x", "recommendation": "y", "forecast": "z"})
    assert "Что получается хорошо" not in out
    assert "Над чем работаем" not in out


def test_strength_slot_renders_when_present():
    out = render(facts(), "t1", {"progress": "x", "strength": "Хорошо идёт Algebra.",
                                 "recommendation": "y", "forecast": "z"})
    assert "✅ Что получается хорошо: Хорошо идёт Algebra." in out


def test_t4_renders_forecast_as_a_goal_not_a_prediction():
    out = render(facts(), "t4", {"observation": "Verbal проседает.",
                                 "recommendation": "10 слов в день.",
                                 "forecast": "Поднять Verbal до 20/27."})
    assert "Цель на следующую неделю" in out
    assert "Прогноз" not in out


def test_t5_renders_proposal_block():
    out = render(facts(), "t5", {"observation": "Не сдал два ДЗ.",
                                 "cause": "Возможно, нагрузка в школе.",
                                 "proposal": "Созвониться втроём."})
    assert "Созвониться втроём." in out


def test_t2_renders_talk_activity():
    out = render(facts(test=None, talk={
        "lessons": 3, "lessons_spoke": 3, "avg_seconds": 75,
        "questions": 4, "answers": 2,
    }), "t2", {"activity": "Активно участвует.", "progress": "x", "recommendation": "y"})
    assert "Активно участвует." in out


def test_curator_name_is_signed_when_known():
    out = render(facts(), "t1", {"progress": "x", "recommendation": "y", "forecast": "z"},
                 curator_name="Айгерим")
    assert "Айгерим" in out


def test_warning_line_when_platform_was_unavailable():
    out = render(facts(test=None, test_unavailable=True), "t1",
                 {"progress": "x", "recommendation": "y", "forecast": "z"})
    assert "Verbal" not in out
