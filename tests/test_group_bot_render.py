"""The group's regular week and this week's changes, read from the plan and the calendar.

``now`` is Monday 14 September 2026, 11:00 in Almaty (06:00 UTC). Lessons are stored in naive UTC;
the schedule generator's ``time_of_day`` is Almaty wall-clock time.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.services import group_bot_render as render

NOW = datetime(2026, 9, 14, 6, 0)


def _plan(*items):
    return {"start_date": "2026-08-17", "lessons_count": 36,
            "schedule_items": [{"day_of_week": d, "time_of_day": t, "duration_minutes": m} for d, t, m in items]}


MWF_2030 = _plan((0, "20:30", 60), (2, "20:30", 60), (4, "20:30", 60))


def _lesson(day, hour, minute=30, minutes=60, **fields):
    start = datetime(2026, 9, day, hour, minute)
    return SimpleNamespace(start_datetime=start, end_datetime=start + timedelta(minutes=minutes),
                           topic=fields.get("topic"), meeting_url=fields.get("meeting_url"))


def _group(config=MWF_2030, name="August 15 SAT - Бекдәулет", is_over=False):
    return SimpleNamespace(name=name, schedule_config=config, is_over=is_over)


REGULAR = [_lesson(14, 15), _lesson(16, 15), _lesson(18, 15), _lesson(21, 15), _lesson(23, 15), _lesson(25, 15)]


def test_the_timetable_is_the_regular_week_not_a_list_of_dates():
    text = render.schedule_answer(_group(), REGULAR, NOW, "ru")
    assert text.splitlines()[:3] == [
        "📚 <b>August 15 SAT - Бекдәулет</b>",
        "🗓 Расписание группы (время Алматы):",
        "• Пн, Ср, Пт — 20:30–21:30",
    ]
    assert "Изменения" not in text and "сентября" not in text
    assert text.endswith("Ближайшие уроки с датами — /lessons")


def test_different_times_on_different_days_are_separate_lines():
    plan = _plan((0, "17:00", 60), (1, "18:00", 60), (2, "17:00", 60), (3, "18:00", 60))
    lines = render.pattern_lines(render.config_slots(plan), "ru")
    assert lines == ["• Пн, Ср — 17:00–18:00", "• Вт, Чт — 18:00–19:00"]


def test_three_days_in_a_row_read_as_a_range():
    plan = _plan(*[(d, "18:00", 60) for d in range(6)])
    assert render.pattern_lines(render.config_slots(plan), "ru") == ["• Пн–Сб — 18:00–19:00"]
    assert render.pattern_lines(render.config_slots(plan), "en") == ["• Mon–Sat — 18:00–19:00"]


def test_a_lesson_moved_to_another_time_the_same_day():
    moved = [_lesson(14, 15), _lesson(16, 14, 0), _lesson(18, 15), _lesson(21, 15)]
    text = render.schedule_answer(_group(), moved, NOW, "ru")
    assert "⚠️ Изменения в ближайшие 7 дней:" in text
    assert "• Ср, 16 сентября — 19:00–20:00 вместо 20:30" in text


def test_a_slot_without_its_lesson_and_an_extra_lesson():
    lessons = [_lesson(14, 15), _lesson(18, 15), _lesson(19, 10, 0), _lesson(21, 15)]
    text = render.schedule_answer(_group(), lessons, NOW, "ru")
    assert "• Ср, 16 сентября, 20:30 — урока не будет" in text
    assert "• Сб, 19 сентября, 15:00–16:00 — дополнительный урок" in text


def test_today_is_called_today():
    lessons = [_lesson(16, 15), _lesson(18, 15), _lesson(21, 15)]
    text = render.schedule_answer(_group(), lessons, NOW, "ru")
    assert "• Сегодня, 14 сентября, 20:30 — урока не будет" in text


def test_the_week_after_the_last_lesson_is_not_reported_as_missing_lessons():
    text = render.schedule_answer(_group(), [_lesson(14, 15), _lesson(16, 15)], NOW, "ru")
    assert "Изменения" not in text


def test_a_passed_slot_today_is_not_missing():
    later = datetime(2026, 9, 14, 17, 0)        # 22:00 in Almaty, the 20:30 lesson is over
    lessons = [_lesson(16, 15), _lesson(18, 15), _lesson(21, 15)]
    assert "Изменения" not in render.schedule_answer(_group(), lessons, later, "ru")


def test_a_plan_the_calendar_has_left_behind_gives_way_to_the_calendar():
    stale = _plan((1, "18:00", 60), (3, "18:00", 60))
    text = render.schedule_answer(_group(stale), REGULAR, NOW, "ru")
    assert "• Пн, Ср, Пт — 20:30–21:30" in text and "Вт" not in text


def test_a_group_without_a_plan_reads_its_week_off_the_calendar():
    text = render.schedule_answer(_group(None), REGULAR, NOW, "ru")
    assert "• Пн, Ср, Пт — 20:30–21:30" in text


def test_a_course_that_starts_later_says_when():
    later = [_lesson(28, 15), _lesson(30, 15)]
    text = render.schedule_answer(_group(), later, NOW, "ru")
    assert "Первый урок: Пн, 28 сентября, 20:30–21:30" in text


def test_a_finished_group_says_so():
    assert "Курс группы завершён" in render.schedule_answer(_group(is_over=True), [], NOW, "ru")


def test_the_name_is_escaped():
    text = render.schedule_answer(_group(name="A <b> & C"), REGULAR, NOW, "ru")
    assert text.startswith("📚 <b>A &lt;b&gt; &amp; C</b>")


def test_kazakh_and_english():
    assert "🗓 Топтың сабақ кестесі (Алматы уақыты):\n• Дс, Ср, Жм — 20:30–21:30" in render.schedule_answer(
        _group(), REGULAR, NOW, "kk")
    assert "🗓 Group schedule (Almaty time):\n• Mon, Wed, Fri — 20:30–21:30" in render.schedule_answer(
        _group(), REGULAR, NOW, "en")


def test_language_follows_the_question():
    assert render.language("когда урок?") == "ru"
    assert render.language("келесі сабақ қашан?") == "kk"
    assert render.language("келеси сабак кашан") == "kk"
    assert render.language("when is the next lesson?") == "en"
    assert render.language("когда weekly mock?") == "ru"


def test_dates_say_today_and_tomorrow():
    assert render.when(datetime(2026, 9, 14, 15, 30), datetime(2026, 9, 14, 16, 30), NOW, "ru") == \
        "Сегодня, 14 сентября, 20:30–21:30"
    assert render.when(datetime(2026, 9, 15, 15, 30), None, NOW, "en") == "Tomorrow, Sep 15, 20:30"
    assert render.when(datetime(2026, 9, 16, 15, 30), None, NOW, "kk") == "Ср, 16 қыркүйек, 20:30"
