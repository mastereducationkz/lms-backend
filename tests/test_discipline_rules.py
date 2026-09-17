"""The register's rules: which half-month a day belongs to, and what a lesson costs.

The owner's rule took effect on 16.09.2026, so nothing earlier is ever judged. Every whole minute
late, or cut short, costs 300 ₸; a lesson never taught is a miss a head teacher prices by hand.
Minutes are rounded down, in the teacher's favour, exactly as the students' rule rounds.
"""
from datetime import date, datetime, timedelta

from src.discipline.rules import (
    FINE_PER_MINUTE,
    RULE_START,
    Finding,
    fine_for,
    judge_lesson,
    period_containing,
    periods_until,
    shift_period,
)

LESSON_START = datetime(2026, 9, 17, 13, 0)  # 18:00 Almaty
LESSON_END = datetime(2026, 9, 17, 14, 0)


def test_the_first_period_starts_the_day_the_rule_did():
    assert RULE_START == date(2026, 9, 16)
    first = period_containing(date(2026, 9, 17))
    assert (first.start, first.end) == (date(2026, 9, 16), date(2026, 9, 30))
    assert first.label == "16–30 September 2026"
    assert first.key == "2026-09-16"


def test_a_month_splits_at_the_fifteenth_for_payroll():
    first_half = period_containing(date(2026, 10, 3))
    second_half = period_containing(date(2026, 10, 20))
    assert (first_half.start, first_half.end) == (date(2026, 10, 1), date(2026, 10, 15))
    assert (second_half.start, second_half.end) == (date(2026, 10, 16), date(2026, 10, 31))


def test_february_ends_where_february_ends():
    assert period_containing(date(2027, 2, 20)).end == date(2027, 2, 28)


def test_there_is_nothing_before_the_rule_started():
    assert period_containing(date(2026, 9, 10)) is None
    assert shift_period(period_containing(date(2026, 9, 17)), -1) is None
    assert [p.key for p in periods_until(date(2026, 10, 3))] == ["2026-09-16", "2026-10-01"]


def test_moving_between_periods():
    september = period_containing(date(2026, 9, 17))
    october = shift_period(september, 1)
    assert october.key == "2026-10-01"
    assert shift_period(october, -1).key == september.key


def test_a_teacher_who_joined_on_time_owes_nothing():
    assert judge_lesson(start=LESSON_START, end=LESSON_END, first_join=LESSON_START,
                        last_leave=LESSON_END, measurable=True) == []


def test_each_whole_minute_late_costs_300_tenge():
    findings = judge_lesson(start=LESSON_START, end=LESSON_END,
                            first_join=LESSON_START.replace(minute=3, second=40),
                            last_leave=LESSON_END, measurable=True)
    assert findings == [Finding(kind="late", minutes=3, fine=900, made_up=False)]


def test_seconds_never_round_against_the_teacher():
    findings = judge_lesson(start=LESSON_START, end=LESSON_END,
                            first_join=LESSON_START.replace(minute=0, second=59),
                            last_leave=LESSON_END, measurable=True)
    assert findings == []


def test_time_made_up_is_shown_but_still_proposed():
    findings = judge_lesson(start=LESSON_START, end=LESSON_END,
                            first_join=LESSON_START.replace(minute=3),
                            last_leave=LESSON_END.replace(minute=3), measurable=True)
    assert findings == [Finding(kind="late", minutes=3, fine=900, made_up=True)]


def test_leaving_at_the_bell_after_a_late_start_is_not_made_up():
    findings = judge_lesson(start=LESSON_START, end=LESSON_END,
                            first_join=LESSON_START.replace(minute=3),
                            last_leave=LESSON_END, measurable=True)
    assert findings == [Finding(kind="late", minutes=3, fine=900, made_up=False)]


def test_a_lesson_cut_short_costs_the_same_per_minute():
    findings = judge_lesson(start=LESSON_START, end=LESSON_END, first_join=LESSON_START,
                            last_leave=LESSON_END - timedelta(minutes=8), measurable=True)
    assert findings == [Finding(kind="ended_early", minutes=8, fine=2400, made_up=False)]


def test_late_and_cut_short_are_both_owed():
    findings = judge_lesson(start=LESSON_START, end=LESSON_END,
                            first_join=LESSON_START.replace(minute=2),
                            last_leave=LESSON_END - timedelta(minutes=10), measurable=True)
    assert [(f.kind, f.minutes, f.fine) for f in findings] == [
        ("late", 2, 600), ("ended_early", 10, 3000)]


def test_a_lesson_never_taught_is_a_miss_a_person_prices():
    findings = judge_lesson(start=LESSON_START, end=LESSON_END, first_join=None,
                            last_leave=None, measurable=True)
    assert findings == [Finding(kind="miss", minutes=60, fine=None, made_up=False)]


def test_a_lesson_the_lms_could_not_watch_is_judged_by_nobody():
    assert judge_lesson(start=LESSON_START, end=LESSON_END, first_join=None,
                        last_leave=None, measurable=False) == []


def test_the_rate_is_one_number():
    assert fine_for(7) == 7 * FINE_PER_MINUTE == 2100
