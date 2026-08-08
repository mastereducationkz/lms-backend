"""Tests for the centralized SAT official-date registry (src/assignments/exam_dates.py).

Covers the College Board provenance rules, confirmed-vs-anticipated status, ordering,
and — critically — the "no future known date" boundary, which previously returned the
LAST PAST date and silently produced a negative countdown.

Pure functions, no database: these run everywhere.
"""
from datetime import date

import pytest

from src.assignments.exam_dates import (
    SAT_ANTICIPATED_TEST_DATES,
    SAT_OFFICIAL_TEST_DATES,
    SAT_TEST_DATES,
    SAT_DATES_SOURCE_URL,
    SAT_DATES_VERIFIED_AT,
    SatTestDate,
    format_sat_label,
    get_nearest_sat_date,
    get_sat_test_date,
    parse_month_name,
    parse_sat_target_date,
    resolve_legacy_sat_month,
)


# --------------------------------------------------------------------------------------
# Provenance and published data
# --------------------------------------------------------------------------------------

def test_source_url_is_college_board_dates_page():
    assert SAT_DATES_SOURCE_URL == "https://satsuite.collegeboard.org/sat/dates-deadlines"


def test_verified_at_is_a_real_date():
    assert isinstance(SAT_DATES_VERIFIED_AT, date)


def test_confirmed_2026_27_dates_match_college_board_exactly():
    """The eight published 2026-27 dates, verbatim from College Board."""
    expected = [
        date(2026, 8, 22),
        date(2026, 9, 12),
        date(2026, 10, 3),
        date(2026, 11, 7),
        date(2026, 12, 5),
        date(2027, 3, 6),
        date(2027, 5, 1),
        date(2027, 6, 5),
    ]
    got = [d.test_date for d in SAT_TEST_DATES
           if d.status == "confirmed" and date(2026, 7, 1) <= d.test_date <= date(2027, 7, 1)]
    assert got == expected


def test_anticipated_2027_28_dates_are_present_and_flagged():
    """College Board labels these 'Anticipated 2027-28 Test Dates' - never 'official'."""
    expected = [
        date(2027, 8, 28),
        date(2027, 9, 18),
        date(2027, 10, 2),
        date(2027, 11, 6),
        date(2027, 12, 4),
        date(2028, 3, 4),
        date(2028, 5, 6),
        date(2028, 6, 3),
    ]
    assert SAT_ANTICIPATED_TEST_DATES == expected
    for d in SAT_TEST_DATES:
        if d.test_date in expected:
            assert d.status == "anticipated"


def test_registration_deadlines_for_published_2026_27_dates():
    """Registration + change deadlines as published alongside the 2026-27 schedule."""
    expected = {
        date(2026, 8, 22): (date(2026, 8, 7), date(2026, 8, 11)),
        date(2026, 9, 12): (date(2026, 8, 28), date(2026, 9, 1)),
        date(2026, 10, 3): (date(2026, 9, 18), date(2026, 9, 22)),
        date(2026, 11, 7): (date(2026, 10, 23), date(2026, 10, 27)),
        date(2026, 12, 5): (date(2026, 11, 20), date(2026, 11, 24)),
        date(2027, 3, 6): (date(2027, 2, 19), date(2027, 2, 23)),
        date(2027, 5, 1): (date(2027, 4, 16), date(2027, 4, 20)),
        date(2027, 6, 5): (date(2027, 5, 21), date(2027, 5, 25)),
    }
    for test_date, (reg, change) in expected.items():
        entry = get_sat_test_date(test_date)
        assert entry is not None, f"missing {test_date}"
        assert entry.registration_deadline == reg
        assert entry.change_deadline == change


def test_historical_dates_have_no_deadlines_but_stay_confirmed():
    """2025-26 dates already happened and are no longer published with deadlines."""
    entry = get_sat_test_date(date(2026, 3, 14))
    assert entry is not None
    assert entry.status == "confirmed"
    assert entry.registration_deadline is None


# --------------------------------------------------------------------------------------
# Ordering, dedup, and the legacy list contract
# --------------------------------------------------------------------------------------

def test_all_dates_are_sorted_and_deduplicated():
    all_dates = [d.test_date for d in SAT_TEST_DATES]
    assert all_dates == sorted(all_dates)
    assert len(all_dates) == len(set(all_dates))


def test_status_is_only_confirmed_or_anticipated():
    assert {d.status for d in SAT_TEST_DATES} == {"confirmed", "anticipated"}


def test_legacy_official_list_contains_confirmed_dates_only():
    """SAT_OFFICIAL_TEST_DATES is the back-compat surface every existing consumer imports.

    Anticipated dates must NOT leak into it, or they would appear in planned-date
    pickers and countdowns as though College Board had confirmed them.
    """
    confirmed = [d.test_date for d in SAT_TEST_DATES if d.status == "confirmed"]
    assert SAT_OFFICIAL_TEST_DATES == confirmed
    for anticipated in SAT_ANTICIPATED_TEST_DATES:
        assert anticipated not in SAT_OFFICIAL_TEST_DATES


def test_entries_are_sat_test_date_instances():
    assert all(isinstance(d, SatTestDate) for d in SAT_TEST_DATES)


def test_get_sat_test_date_returns_none_for_unknown_date():
    assert get_sat_test_date(date(2026, 1, 1)) is None


# --------------------------------------------------------------------------------------
# get_nearest_sat_date - boundaries and the exhausted-list behaviour
# --------------------------------------------------------------------------------------

def test_nearest_returns_the_next_future_date():
    assert get_nearest_sat_date(date(2026, 8, 1)) == date(2026, 8, 22)


def test_nearest_is_inclusive_of_the_exam_day_itself():
    """A student sitting the exam today should still see today's date, not the next one."""
    assert get_nearest_sat_date(date(2026, 8, 22)) == date(2026, 8, 22)


def test_nearest_rolls_over_the_day_after_an_exam():
    assert get_nearest_sat_date(date(2026, 8, 23)) == date(2026, 9, 12)


def test_nearest_never_returns_an_anticipated_date():
    """After the last confirmed date, we must not present an anticipated date as official."""
    result = get_nearest_sat_date(date(2027, 6, 6))
    assert result is None


def test_nearest_returns_none_when_no_future_confirmed_date_exists():
    """REGRESSION: previously returned max(SAT_OFFICIAL_TEST_DATES) - a PAST date -
    which downstream rendered as a negative 'days until exam' countdown."""
    assert get_nearest_sat_date(date(2099, 1, 1)) is None


def test_nearest_never_returns_a_past_date():
    for reference in (date(2026, 1, 1), date(2027, 1, 1), date(2027, 6, 30), date(2030, 1, 1)):
        result = get_nearest_sat_date(reference)
        assert result is None or result >= reference


# --------------------------------------------------------------------------------------
# Month parsing / legacy cohort strings
# --------------------------------------------------------------------------------------

def test_parse_month_name_handles_abbreviations_and_punctuation():
    assert parse_month_name("August") == 8
    assert parse_month_name("  august ") == 8
    assert parse_month_name("Sept") == 9
    assert parse_month_name("Sept.") == 9
    assert parse_month_name("nonsense") is None


def test_parse_sat_target_date_accepts_iso():
    assert parse_sat_target_date("2026-10-03") == date(2026, 10, 3)


def test_parse_sat_target_date_accepts_the_label_format_it_writes_back():
    """format_sat_label output must round-trip through the parser."""
    label = format_sat_label(date(2026, 10, 3))
    assert parse_sat_target_date(label) == date(2026, 10, 3)


def test_parse_sat_target_date_returns_none_for_empty():
    assert parse_sat_target_date(None) is None
    assert parse_sat_target_date("") is None
    assert parse_sat_target_date("   ") is None


def test_legacy_bare_month_resolves_to_the_official_date_in_that_month():
    """The 'August / September / October' cohort strings students already picked."""
    assert resolve_legacy_sat_month("August", reference_date=date(2026, 7, 1)) == date(2026, 8, 22)
    assert resolve_legacy_sat_month("September", reference_date=date(2026, 7, 1)) == date(2026, 9, 12)
    assert resolve_legacy_sat_month("October", reference_date=date(2026, 7, 1)) == date(2026, 10, 3)


def test_legacy_bare_month_is_not_silently_wrong_by_a_day():
    """REGRESSION: the frontend clone projected 2025-26 day-of-month onto 2026,
    producing Aug 23 / Sep 13 / Oct 4 instead of Aug 22 / Sep 12 / Oct 3."""
    assert resolve_legacy_sat_month("August", reference_date=date(2026, 7, 1)) != date(2026, 8, 23)
    assert resolve_legacy_sat_month("October", reference_date=date(2026, 7, 1)) != date(2026, 10, 4)


def test_parse_sat_target_date_falls_back_to_legacy_month_resolution():
    assert parse_sat_target_date("October", reference_date=date(2026, 7, 1)) == date(2026, 10, 3)


def test_format_sat_label_is_stable():
    assert format_sat_label(date(2026, 8, 22)) == "Aug 22, 2026"
