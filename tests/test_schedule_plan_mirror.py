"""The two schedule rules, without a database.

Rauan's group is the worked example throughout: 14 lessons taught, then Mon/Fri 18:00–19:00 and
Sat/Sun 19:00–20:30 Almaty, 32 lessons in the course.
"""
from datetime import date, datetime, timedelta, timezone

from src.services.schedule_plan import (
    LessonSpan,
    future_schedule_slots,
    is_counted_schedule,
    plan_schedule_changes,
    weekly_slot_minutes,
)

UTC = timezone.utc
KZ = timezone(timedelta(hours=5))
NOW = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)  # Wed 16.09 23:00 Almaty

RAUAN = {
    "start_date": "2026-08-25",
    "lessons_count": 32,
    "weeks_count": 3,  # stale: must not truncate a counted schedule
    "schedule_items": [
        {"day_of_week": 0, "time_of_day": "18:00", "duration_minutes": 60},
        {"day_of_week": 4, "time_of_day": "18:00", "duration_minutes": 60},
        {"day_of_week": 5, "time_of_day": "19:00", "duration_minutes": 90},
        {"day_of_week": 6, "time_of_day": "19:00", "duration_minutes": 90},
    ],
}


def kz(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=KZ).astimezone(UTC)


def test_rauan_gets_the_18_lessons_the_course_still_needs():
    slots = future_schedule_slots(RAUAN, started=14, now=NOW)

    assert len(slots) == 18
    assert slots[0] == (kz(2026, 9, 18, 18), 60)
    assert slots[1] == (kz(2026, 9, 19, 19), 90)
    assert slots[-1] == (kz(2026, 10, 17, 19), 90)
    assert sum(m for _, m in slots) == 1350  # 22 h 30 min


def test_the_count_comes_from_lessons_taught_not_from_the_pattern_replayed_since_start():
    # The old rule replayed the new pattern from 25.08 (12 slots "past") and scheduled 24.
    assert len(future_schedule_slots({**RAUAN, "lessons_count": 36}, started=14, now=NOW)) == 22


def test_nothing_is_scheduled_once_the_course_is_taught():
    assert future_schedule_slots(RAUAN, started=32, now=NOW) == []
    assert future_schedule_slots(RAUAN, started=40, now=NOW) == []


def test_a_future_start_date_is_respected():
    cfg = {**RAUAN, "start_date": "2026-10-01", "lessons_count": 3}
    assert [dt for dt, _ in future_schedule_slots(cfg, started=0, now=NOW)] == [
        kz(2026, 10, 2, 18), kz(2026, 10, 3, 19), kz(2026, 10, 4, 19)
    ]


def test_a_slot_later_today_is_still_future_and_an_earlier_one_is_not():
    cfg = {"start_date": "2026-09-16", "lessons_count": 2, "schedule_items": [
        {"day_of_week": 2, "time_of_day": "22:00"},
        {"day_of_week": 2, "time_of_day": "23:30"},
    ]}
    now = kz(2026, 9, 16, 23, 0)
    assert [dt for dt, _ in future_schedule_slots(cfg, started=0, now=now)] == [
        kz(2026, 9, 16, 23, 30), kz(2026, 9, 23, 22)
    ]


def test_excluded_slots_and_approved_cancellations_are_skipped_not_consumed():
    cfg = {**RAUAN, "lessons_count": 16, "excluded_slot_keys": ["2026-09-19_19:00"]}
    skip = [kz(2026, 9, 20, 19).replace(tzinfo=None)]  # naive UTC, as events store it
    slots = [dt for dt, _ in future_schedule_slots(cfg, started=14, now=NOW, skip_instants=skip)]
    assert slots == [kz(2026, 9, 18, 18), kz(2026, 9, 21, 18)]


def test_a_schedule_without_a_count_keeps_the_weeks_based_expansion():
    cfg = {"start_date": "2026-09-14", "weeks_count": 2,
           "schedule_items": [{"day_of_week": 0, "time_of_day": "10:00"}]}
    assert not is_counted_schedule(cfg)
    assert future_schedule_slots(cfg, started=5, now=NOW) == [(kz(2026, 9, 21, 10), 60)]


def test_a_schedule_without_a_count_still_skips_approved_cancellations():
    """The LMS only switches a cancelled lesson off; without a count nothing else remembers it."""
    cfg = {"start_date": "2026-09-14", "weeks_count": 2, "schedule_items": [
        {"day_of_week": 0, "time_of_day": "10:00"},
        {"day_of_week": 2, "time_of_day": "10:00"},
    ]}
    # Naive UTC as events store it, with stray seconds: compared at minute precision.
    cancelled_wednesday = (kz(2026, 9, 23, 10) + timedelta(seconds=20)).replace(tzinfo=None)

    assert future_schedule_slots(
        cfg, started=0, now=NOW, skip_instants=[cancelled_wednesday]
    ) == [(kz(2026, 9, 21, 10), 60)]


def test_weekly_slot_minutes_reads_each_day_and_defaults_to_an_hour():
    cfg = {"schedule_items": [
        {"day_of_week": 5, "time_of_day": "19:00", "duration_minutes": 90},
        {"day_of_week": 0, "time_of_day": "18:00"},
    ]}
    assert weekly_slot_minutes(cfg) == {(5, "19:00"): 90, (0, "18:00"): 60}
    assert weekly_slot_minutes(None) == {}


def test_a_duplicate_slot_keeps_the_first_items_length_everywhere():
    cfg = {"schedule_items": [
        {"day_of_week": 5, "time_of_day": "19:00", "duration_minutes": 90},
        {"day_of_week": 5, "time_of_day": "19:00", "duration_minutes": 60},
    ]}
    assert weekly_slot_minutes(cfg) == {(5, "19:00"): 90}

    counted = {**cfg, "start_date": "2026-09-16", "lessons_count": 1}
    assert future_schedule_slots(counted, started=0, now=NOW) == [(kz(2026, 9, 19, 19), 90)]


def test_now_is_compared_at_full_precision_naive_or_aware():
    cfg = {"start_date": "2026-09-16", "lessons_count": 1, "schedule_items": [
        {"day_of_week": 2, "time_of_day": "18:00"},
    ]}
    at = kz(2026, 9, 16, 18)  # Wed 18:00 KZ — matches day_of_week 2, and is now 45s in the past
    now_aware = at + timedelta(seconds=45)
    now_naive = now_aware.replace(tzinfo=None)

    aware_result = future_schedule_slots(cfg, started=0, now=now_aware)
    naive_result = future_schedule_slots(cfg, started=0, now=now_naive)

    assert aware_result == naive_result
    assert at not in [dt for dt, _ in aware_result]


def span(event_id, start, minutes):
    return LessonSpan(event_id, start.replace(tzinfo=None), (start + timedelta(minutes=minutes)).replace(tzinfo=None))


def test_a_lesson_already_on_a_slot_stays_there_and_the_rest_move_in_order():
    events = [
        span(1, kz(2026, 9, 18, 18), 60),  # Fri, on a slot
        span(2, kz(2026, 9, 23, 18), 60),  # Wed, no longer a slot
        span(3, kz(2026, 9, 25, 18), 60),  # Fri, on a slot
        span(4, kz(2026, 9, 27, 18), 60),  # Sun 18:00, slot is now 19:00
    ]
    desired = [(kz(2026, 9, 18, 18), 60), (kz(2026, 9, 19, 19), 90),
               (kz(2026, 9, 25, 18), 60), (kz(2026, 9, 26, 19), 90)]

    changes = plan_schedule_changes(events, desired, previous_minutes={})

    assert [(c.kind, c.event_id, c.start) for c in changes] == [
        ("keep", 1, kz(2026, 9, 18, 18)),
        ("move", 2, kz(2026, 9, 19, 19)),
        ("keep", 3, kz(2026, 9, 25, 18)),
        ("move", 4, kz(2026, 9, 26, 19)),
    ]
    assert changes[1].end - changes[1].start == timedelta(minutes=90)
    assert changes[1].previous_start == kz(2026, 9, 23, 18)


def test_leftover_slots_are_created_and_leftover_lessons_switched_off():
    events = [span(1, kz(2026, 10, 12, 18), 60), span(2, kz(2026, 10, 14, 18), 60)]
    desired = [(kz(2026, 10, 12, 18), 60)]
    assert [(c.kind, c.event_id) for c in plan_schedule_changes(events, desired, {})] == [
        ("keep", 1), ("deactivate", 2)
    ]
    created = plan_schedule_changes([], [(kz(2026, 10, 16, 18), 60)], {})
    assert [(c.kind, c.event_id, c.end) for c in created] == [("create", None, kz(2026, 10, 16, 19))]


def test_a_hand_shortened_lesson_survives_a_save_that_did_not_change_its_day():
    shortened = span(7, kz(2026, 10, 17, 19), 60)
    desired = [(kz(2026, 10, 17, 19), 90)]
    change = plan_schedule_changes([shortened], desired, previous_minutes={(5, "19:00"): 90})[0]
    assert (change.kind, change.end) == ("keep", kz(2026, 10, 17, 20))


def test_changing_a_days_length_resizes_its_lessons():
    lesson = span(7, kz(2026, 10, 17, 19), 60)
    desired = [(kz(2026, 10, 17, 19), 90)]
    change = plan_schedule_changes([lesson], desired, previous_minutes={(5, "19:00"): 60})[0]
    assert (change.kind, change.end) == ("resize", kz(2026, 10, 17, 20, 30))


def test_without_previous_minutes_the_slot_decides_as_it_always_did():
    lesson = span(7, kz(2026, 10, 17, 19), 60)
    change = plan_schedule_changes([lesson], [(kz(2026, 10, 17, 19), 90)], None)[0]
    assert change.kind == "resize"


def test_seconds_on_a_stored_start_do_not_break_an_exact_match():
    at = kz(2026, 9, 18, 18)
    ev = LessonSpan(1, (at + timedelta(seconds=30)).replace(tzinfo=None), (at + timedelta(minutes=60)).replace(tzinfo=None))
    assert plan_schedule_changes([ev], [(at, 60)], {})[0].kind == "keep"
