"""What the register counts, and what it costs.

The owner's rule, in force since 16.09.2026: every whole minute a teacher is late, or cuts a lesson
short, costs 200 ₸; a lesson never taught is a miss that a head teacher prices by hand. Minutes are
rounded down, in the teacher's favour, exactly as the students' rule rounds.

Nothing here reads a clock or a database, so the whole rule is testable in one file — and changing
the rate or the starting date is one line, not a migration.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta

#: The day the rule took effect. Nothing earlier is judged, counted, fined or exported.
RULE_START = date(2026, 9, 16)

#: ₸ for every whole minute late, or cut short.
FINE_PER_MINUTE = 200

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


@dataclass(frozen=True)
class Period:
    """Half a month: 1–15 or 16 to the month's end — the payroll cycle (the 15th and the 30/31st)."""

    start: date
    end: date

    @property
    def key(self) -> str:
        return self.start.isoformat()

    @property
    def label(self) -> str:
        return f"{self.start.day}–{self.end.day} {_MONTHS[self.start.month - 1]} {self.start.year}"

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def _half_of(day: date) -> Period:
    """The plain half-month `day` sits in, before the rule's start date is applied."""
    if day.day <= 15:
        return Period(day.replace(day=1), day.replace(day=15))
    return Period(day.replace(day=16), day.replace(day=calendar.monthrange(day.year, day.month)[1]))


def period_containing(day: date) -> Period | None:
    """The half-month `day` falls in, or None for a day before the rule started."""
    if day < RULE_START:
        return None
    half = _half_of(day)
    return Period(max(half.start, RULE_START), half.end)


def shift_period(period: Period | None, step: int) -> Period | None:
    """The neighbouring period, or None once it would fall before the rule started."""
    if period is None:
        return None
    day = period.start
    for _ in range(abs(step)):
        day = (day - timedelta(days=1)) if step < 0 else (_half_of(day).end + timedelta(days=1))
        if day < RULE_START:
            return None
    return period_containing(day)


def periods_until(day: date) -> list[Period]:
    """Every period from the rule's first day to the one containing `day`, oldest first."""
    periods: list[Period] = []
    current = period_containing(RULE_START)
    while current is not None and current.start <= day:
        periods.append(current)
        current = shift_period(current, 1)
    return periods


@dataclass(frozen=True)
class Finding:
    """One thing owed for one lesson. ``fine`` is None when a person must price it."""

    kind: str  # "late" | "ended_early" | "miss"
    minutes: int
    fine: int | None
    made_up: bool


def fine_for(minutes: int) -> int:
    return max(0, minutes) * FINE_PER_MINUTE


def _whole_minutes(seconds: float) -> int:
    return max(0, int(seconds // 60))


def judge_lesson(*, start: datetime, end: datetime, first_join: datetime | None,
                 last_leave: datetime | None, measurable: bool) -> list[Finding]:
    """What one lesson owes, from its timetable and the teacher's time in the room.

    ``measurable`` is False when the lesson had no LMS Meet room: the LMS saw nothing, so it judges
    nothing. Silence is not evidence of a lesson taught, and a fine has to rest on evidence.
    """
    if not measurable:
        return []
    if first_join is None:
        return [Finding(kind="miss", minutes=_whole_minutes((end - start).total_seconds()),
                        fine=None, made_up=False)]

    findings: list[Finding] = []
    late = _whole_minutes((first_join - start).total_seconds()) if first_join > start else 0
    leave = last_leave or first_join
    early = _whole_minutes((end - leave).total_seconds()) if leave < end else 0
    if late:
        # Made up: stayed at least as far past the end as the start was missed.
        findings.append(Finding(kind="late", minutes=late, fine=fine_for(late),
                                made_up=leave >= end + (first_join - start)))
    if early:
        findings.append(Finding(kind="ended_early", minutes=early, fine=fine_for(early), made_up=False))
    return findings
