from datetime import date, datetime
from typing import Optional

SAT_OFFICIAL_TEST_DATES = [
    date(2025, 8, 23),
    date(2025, 9, 13),
    date(2025, 10, 4),
    date(2025, 11, 8),
    date(2025, 12, 6),
    date(2026, 3, 14),
    date(2026, 5, 2),
    date(2026, 6, 6),
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


def get_nearest_sat_date(reference_date: Optional[date] = None) -> date:
    reference = reference_date or date.today()
    future_dates = [d for d in SAT_OFFICIAL_TEST_DATES if d >= reference]
    if future_dates:
        return min(future_dates)
    return max(SAT_OFFICIAL_TEST_DATES)


def format_sat_label(value: date) -> str:
    return value.strftime("%b %d, %Y")


def parse_month_name(value: str) -> Optional[int]:
    normalized = value.strip().lower().replace(".", "")
    return MONTH_ALIASES.get(normalized)


def resolve_legacy_sat_month(value: str, reference_date: Optional[date] = None) -> date:
    month = parse_month_name(value)
    if month is None:
        return get_nearest_sat_date(reference_date)

    candidates = [d for d in SAT_OFFICIAL_TEST_DATES if d.month == month]
    if not candidates:
        return get_nearest_sat_date(reference_date)

    reference = reference_date or date.today()
    future_candidates = [d for d in candidates if d >= reference]
    if future_candidates:
        return min(future_candidates)
    return max(candidates)


def parse_sat_target_date(value: Optional[str], reference_date: Optional[date] = None) -> Optional[date]:
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
