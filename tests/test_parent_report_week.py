"""Границы недели для родительских отчётов: Алматы (UTC+5) → наивный UTC.

Без базы: это чистая арифметика дат, и она должна проверяться там, где Postgres не нужен.
"""
from datetime import date, datetime

from src.reports.parent.week import week_bounds, week_utc_range


def test_week_bounds_from_midweek_day():
    # 2026-09-18 — пятница.
    assert week_bounds(date(2026, 9, 18)) == (date(2026, 9, 14), date(2026, 9, 20))


def test_week_bounds_on_monday_returns_that_monday():
    assert week_bounds(date(2026, 9, 14)) == (date(2026, 9, 14), date(2026, 9, 20))


def test_week_bounds_on_sunday_stays_in_the_same_week():
    assert week_bounds(date(2026, 9, 20)) == (date(2026, 9, 14), date(2026, 9, 20))


def test_week_utc_range_shifts_back_five_hours():
    # Понедельник 00:00 в Алматы — это воскресенье 19:00 UTC.
    start, end = week_utc_range(date(2026, 9, 14))
    assert start == datetime(2026, 9, 13, 19, 0, 0)
    assert end == datetime(2026, 9, 20, 19, 0, 0)


def test_week_utc_range_is_half_open_and_exactly_seven_days():
    start, end = week_utc_range(date(2026, 9, 14))
    assert (end - start).days == 7
    assert start.tzinfo is None and end.tzinfo is None
