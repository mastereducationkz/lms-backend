"""How long a lesson actually ran — the multiplier for an hourly rate.

The teacher rates the CRM pushes are **hourly** (`TeacherHourlyRate.group_rate` /
`.individual_rate`), and the payslip multiplied them by the *number* of lessons. A group that
meets for ninety minutes therefore paid exactly what a sixty-minute one did, and the payslip
line said «ставка: 8000 тг/урок» about a rate that is per hour.

A handful of events carry durations no lesson ever had — a few thousand minutes, a few of
twenty — so anything outside a sane range is priced at the ordinary hour rather than taken
literally. Old events with no usable timestamps price exactly as they always did.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

#: What a lesson is worth when its own duration cannot be trusted, and the ordinary length.
DEFAULT_MINUTES = 60
#: Shorter than this is a mis-click, not a lesson.
MIN_MINUTES = 10
#: Longer than a day of teaching. Catches the 1439- and 2519-minute rows.
MAX_MINUTES = 300


def lesson_minutes(start: Optional[datetime], end: Optional[datetime]) -> int:
    """Payable minutes for one lesson, with untrustworthy values folded to the hour."""
    if start is None or end is None:
        return DEFAULT_MINUTES
    minutes = int(round((end - start).total_seconds() / 60))
    if minutes < MIN_MINUTES or minutes > MAX_MINUTES:
        return DEFAULT_MINUTES
    return minutes


def amount_for(hourly_rate: int, total_minutes: int) -> int:
    """`rate × minutes / 60`, rounded to the tenge.

    Minutes are summed by the caller and divided once here, so three fifty-minute lessons are
    worth exactly two and a half hours rather than three rounded thirds.
    """
    exact = (Decimal(hourly_rate) * Decimal(total_minutes)) / Decimal(60)
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_hours(total_minutes: int) -> str:
    """«7,5 ч» — for a payslip line whose lesson count no longer implies its hours."""
    hours = (Decimal(total_minutes) / Decimal(60)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    text = f"{hours:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")
