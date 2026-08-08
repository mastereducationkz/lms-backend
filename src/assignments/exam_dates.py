"""Centralized registry of official SAT test dates.

This module is the single source of truth for SAT test dates across the platform.
Nothing else may hard-code a SAT date: the frontend reads this list over the wire via
``GET /assignment-zero/sat-official-dates`` rather than keeping its own copy.

Provenance
----------
Dates are transcribed from College Board's published schedule at
``SAT_DATES_SOURCE_URL`` and were last checked on ``SAT_DATES_VERIFIED_AT``.

College Board publishes two distinct kinds of date, and conflating them misleads
students:

``confirmed``
    Published as the real schedule, with registration deadlines.

``anticipated``
    Listed by College Board under the heading "Anticipated 2027-28 Test Dates".
    These are provisional and may move. They are deliberately EXCLUDED from
    :data:`SAT_OFFICIAL_TEST_DATES`, so they never appear in planned-date pickers or
    exam countdowns as though they were settled.

Timezone
--------
Every value is a date-only ``datetime.date``. SAT test dates are calendar dates and
must not be shifted by a timezone conversion.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Literal, Optional

SAT_DATES_SOURCE_URL = "https://satsuite.collegeboard.org/sat/dates-deadlines"

# Last time a human compared this file against SAT_DATES_SOURCE_URL.
SAT_DATES_VERIFIED_AT = date(2026, 8, 8)

SatTestDateStatus = Literal["confirmed", "anticipated"]


@dataclass(frozen=True)
class SatTestDate:
    """One official SAT administration.

    ``registration_deadline`` and ``change_deadline`` are ``None`` for past
    administrations, whose deadlines College Board no longer publishes.
    """

    test_date: date
    status: SatTestDateStatus
    registration_deadline: Optional[date] = None
    change_deadline: Optional[date] = None

    @property
    def is_confirmed(self) -> bool:
        return self.status == "confirmed"


# Ordered oldest -> newest. Keep sorted and deduplicated; the tests enforce both.
SAT_TEST_DATES: List[SatTestDate] = [
    # --- 2025-26 school year. Past administrations; College Board no longer
    # publishes their deadlines, but we keep the dates so historical results and
    # legacy bare-month cohort strings still resolve correctly.
    SatTestDate(date(2025, 8, 23), "confirmed"),
    SatTestDate(date(2025, 9, 13), "confirmed"),
    SatTestDate(date(2025, 10, 4), "confirmed"),
    SatTestDate(date(2025, 11, 8), "confirmed"),
    SatTestDate(date(2025, 12, 6), "confirmed"),
    SatTestDate(date(2026, 3, 14), "confirmed"),
    SatTestDate(date(2026, 5, 2), "confirmed"),
    SatTestDate(date(2026, 6, 6), "confirmed"),
    # --- 2026-27 school year. Published schedule with deadlines.
    SatTestDate(date(2026, 8, 22), "confirmed", date(2026, 8, 7), date(2026, 8, 11)),
    SatTestDate(date(2026, 9, 12), "confirmed", date(2026, 8, 28), date(2026, 9, 1)),
    SatTestDate(date(2026, 10, 3), "confirmed", date(2026, 9, 18), date(2026, 9, 22)),
    SatTestDate(date(2026, 11, 7), "confirmed", date(2026, 10, 23), date(2026, 10, 27)),
    SatTestDate(date(2026, 12, 5), "confirmed", date(2026, 11, 20), date(2026, 11, 24)),
    SatTestDate(date(2027, 3, 6), "confirmed", date(2027, 2, 19), date(2027, 2, 23)),
    SatTestDate(date(2027, 5, 1), "confirmed", date(2027, 4, 16), date(2027, 4, 20)),
    SatTestDate(date(2027, 6, 5), "confirmed", date(2027, 5, 21), date(2027, 5, 25)),
    # --- 2027-28 school year. College Board heading: "Anticipated 2027-28 Test Dates".
    # Provisional - deadlines not yet published, and excluded from the official list.
    SatTestDate(date(2027, 8, 28), "anticipated"),
    SatTestDate(date(2027, 9, 18), "anticipated"),
    SatTestDate(date(2027, 10, 2), "anticipated"),
    SatTestDate(date(2027, 11, 6), "anticipated"),
    SatTestDate(date(2027, 12, 4), "anticipated"),
    SatTestDate(date(2028, 3, 4), "anticipated"),
    SatTestDate(date(2028, 5, 6), "anticipated"),
    SatTestDate(date(2028, 6, 3), "anticipated"),
]

# Back-compat surface. Every pre-existing consumer imports this name and expects a
# plain list of dates. CONFIRMED DATES ONLY - see the module docstring.
SAT_OFFICIAL_TEST_DATES: List[date] = [d.test_date for d in SAT_TEST_DATES if d.is_confirmed]

SAT_ANTICIPATED_TEST_DATES: List[date] = [
    d.test_date for d in SAT_TEST_DATES if d.status == "anticipated"
]

MONTH_ALIASES = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "sept": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def get_sat_test_date(value: date) -> Optional[SatTestDate]:
    """Look up the full record (status + deadlines) for a given test date."""
    for entry in SAT_TEST_DATES:
        if entry.test_date == value:
            return entry
    return None


def get_nearest_sat_date(reference_date: Optional[date] = None) -> Optional[date]:
    """The next CONFIRMED SAT date on or after ``reference_date``.

    Returns ``None`` once the confirmed list is exhausted.

    This previously returned ``max(SAT_OFFICIAL_TEST_DATES)`` when nothing was
    upcoming, i.e. a date in the PAST, which callers then rendered as a negative
    "days until your exam" countdown and used to schedule bogus curator tasks.
    Callers must handle ``None`` and say "no upcoming date" instead of inventing one.

    Anticipated dates are never returned - they are not a commitment we can show a
    student as their exam day.
    """
    reference = reference_date or date.today()
    future_dates = [d for d in SAT_OFFICIAL_TEST_DATES if d >= reference]
    if future_dates:
        return min(future_dates)
    return None


def format_sat_label(value: date) -> str:
    return value.strftime("%b %d, %Y")


def parse_month_name(value: str) -> Optional[int]:
    normalized = value.strip().lower().replace(".", "")
    return MONTH_ALIASES.get(normalized)


def resolve_legacy_sat_month(value: str, reference_date: Optional[date] = None) -> Optional[date]:
    """Resolve a legacy bare-month cohort string ("August") to a real official date.

    Students picked a bare month before planned dates were stored as real dates, so
    these strings are still in ``assignment_zero_submissions.sat_target_date``.

    Returns ``None`` only when the month is unparseable AND no confirmed date remains.
    """
    month = parse_month_name(value)
    if month is None:
        return get_nearest_sat_date(reference_date)

    candidates = [d for d in SAT_OFFICIAL_TEST_DATES if d.month == month]
    reference = reference_date or date.today()
    if not candidates:
        # No official date on record for this month; treat a bare month as this
        # year's month (1st is a safe placeholder in the intended year).
        return date(reference.year, month, 1)

    future_candidates = [d for d in candidates if d >= reference]
    if future_candidates:
        return min(future_candidates)
    # Only past candidates exist (e.g. "October" when our official list only covers
    # last year). Keep the official date in the intended year, even though it has
    # passed - the student's stated intent was that specific administration.
    same_year_candidates = [d for d in candidates if d.year == reference.year]
    if same_year_candidates:
        return max(same_year_candidates)

    return date(reference.year, month, 1)


def parse_sat_target_date(value: Optional[str], reference_date: Optional[date] = None) -> Optional[date]:
    """Coerce any stored SAT target representation into a real date.

    Accepts ISO dates, the ``format_sat_label`` output written back by the planned-date
    endpoints, and legacy bare-month cohort strings.
    """
    if value is None:
        return None

    stripped = value.strip()
    if stripped == "":
        return None

    try:
        return date.fromisoformat(stripped)
    except ValueError:
        pass

    supported_formats = [
        "%b %d, %Y",
        "%b. %d, %Y",
        "%B %d, %Y",
    ]
    for fmt in supported_formats:
        try:
            return datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue

    return resolve_legacy_sat_month(stripped, reference_date=reference_date)
