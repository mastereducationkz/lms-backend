"""What one teacher owes for a date range, for the pages inside the LMS that show pay.

The CRM reads the same figures over HTTP (`/internal/crm/discipline/*`); this is the in-process
version for the LMS's own salary breakdown, and both end at :func:`~src.discipline.service.
judged_lessons`, so a teacher reading their LMS payslip and an accountant reading the CRM
cannot be shown different money for the same lessons.

A date range is turned into the half-month periods it touches, never into an arbitrary window:
the rule is defined per period, a period is what a head teacher closes, and a total that
straddled two of them could not say whether it was settled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from src.discipline import service
from src.discipline.rules import Period, period_containing, shift_period
from src.schemas.models import Event


@dataclass(frozen=True)
class Fines:
    """What a teacher owes for a range, and whether the figure can still change."""

    total: int = 0
    late_minutes: int = 0
    early_minutes: int = 0
    misses: int = 0
    #: Findings nobody has priced yet — a missed lesson waits for a head teacher.
    unpriced: int = 0
    #: Of `late_minutes`, the ones given back by staying past the end. Never reduces the fine.
    made_up_minutes: int = 0
    #: True when every period behind the figure is closed, so it may be treated as settled.
    final: bool = True

    @property
    def any(self) -> bool:
        return bool(self.total or self.unpriced)


def periods_between(start: date, end: date) -> list[Period]:
    """The half-months a range touches, oldest first; empty before the rule started."""
    periods: list[Period] = []
    current = period_containing(start if start >= service.RULE_START else service.RULE_START)
    while current is not None and current.start <= end:
        periods.append(current)
        current = shift_period(current, 1)
    return periods


def fines_for(db: Session, teacher_id: int, start: date, end: date,
              now: Optional[datetime] = None) -> Fines:
    """One teacher's fines over a range of Almaty days, inclusive."""
    periods = periods_between(start, end)
    if not periods:
        return Fines()

    now = now or service.now()
    closed = {row.period_key for row in service.closed_periods(db)}
    total = late = early = misses = unpriced = made_up = 0
    all_closed = True

    for period in periods:
        all_closed = all_closed and period.key in closed
        # Scoped to this teacher, so a payslip does not walk the whole school's half-month.
        for lesson in service.judged_lessons(
            db, period, now, scope=(Event.teacher_id == teacher_id)
        ):
            if not (start <= lesson["day"] <= end):
                continue
            for finding in lesson["findings"]:
                amount, is_unpriced = service._amount_of(finding)
                total += amount
                unpriced += int(is_unpriced)
                if finding["kind"] == "late":
                    late += finding["minutes"]
                    if finding.get("made_up"):
                        made_up += finding["minutes"]
                elif finding["kind"] == "ended_early":
                    early += finding["minutes"]
                else:
                    misses += 1

    return Fines(total=total, late_minutes=late, early_minutes=early, misses=misses,
                 unpriced=unpriced, made_up_minutes=made_up, final=all_closed)
