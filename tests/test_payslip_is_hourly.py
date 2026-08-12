"""The payslip a teacher reads must price the hours they actually taught.

`TeacherHourlyRate` is what the CRM pushes and what the payslip quotes, and the payslip
multiplied it by the *number* of lessons — so a ninety-minute group paid exactly what a
sixty-minute one did, while the line read «ставка: 8000 тг/урок» about a rate that is per
hour.
"""
from datetime import datetime, timedelta

import pytest

from src.services.lesson_minutes import (
    DEFAULT_MINUTES,
    amount_for,
    format_hours,
    lesson_minutes,
)


def _lesson(minutes):
    start = datetime(2026, 8, 17, 11, 0)
    return start, start + timedelta(minutes=minutes)


@pytest.mark.parametrize("minutes", [60, 90, 120, 45])
def test_a_lesson_is_worth_its_own_length(minutes):
    assert lesson_minutes(*_lesson(minutes)) == minutes


@pytest.mark.parametrize("minutes", [0, -30, 5, 1439, 2519])
def test_an_untrustworthy_duration_prices_as_an_ordinary_hour(minutes):
    # Real values from the production table. Priced literally, 2519 minutes would pay
    # someone for a forty-two-hour lesson.
    assert lesson_minutes(*_lesson(minutes)) == DEFAULT_MINUTES


def test_missing_timestamps_price_as_before():
    assert lesson_minutes(None, None) == DEFAULT_MINUTES
    assert lesson_minutes(datetime(2026, 8, 17), None) == DEFAULT_MINUTES


def test_an_hour_of_teaching_pays_the_hourly_rate():
    assert amount_for(8000, 60) == 8000


def test_two_ninety_minute_lessons_pay_three_hours():
    # The case the whole change exists for: 2 x 90 = 180 minutes = 3 hours.
    assert amount_for(8000, 180) == 24000


def test_five_ordinary_lessons_pay_exactly_what_they_did():
    # Azamat's «June 10 SAT» line: 5 x 60 minutes at 8000/hour.
    assert amount_for(8000, 5 * 60) == 40000


def test_mixed_durations_are_summed_before_dividing():
    # Three 50-minute lessons are 2.5 hours, not three rounded thirds of an hour.
    assert amount_for(3000, 150) == 7500


def test_hours_are_written_the_way_a_payslip_reads():
    assert format_hours(60) == "1"
    assert format_hours(90) == "1,5"
    assert format_hours(450) == "7,5"
    assert format_hours(180) == "3"
