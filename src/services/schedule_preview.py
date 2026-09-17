"""What a schedule save would do — computed, never written.

The preview behind ``POST /leaderboard/curator/schedule/preview``: the lessons a save of a
config would keep, move, resize, create and switch off, the course totals and the warnings.
It reads through the save's own functions (``load_schedule_state``, ``desired_schedule_slots``,
``plan_schedule_changes``), so it uses the same ``now`` split, lessons-taught count,
approved-cancellation skipping and pairing as :func:`apply_group_schedule` — no second copy of
the rules.

Twin of ``preview_group_schedule`` in the CRM's ``src/groups/schedule_reconciliation.py``, with
the same response keys. One deliberate difference: the CRM's save merges duplicate lessons
before it counts, the LMS save does not, so the duplicates warning says so.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.services.schedule_plan import (
    KZ_TZ,
    LessonSpan,
    is_counted_schedule,
    parse_start_date,
    plan_schedule_changes,
    weekly_slot_minutes,
)
from src.services.schedule_reconciliation import desired_schedule_slots, load_schedule_state


def _as_utc(moment: datetime) -> datetime:
    """An aware UTC instant; naive means UTC (how events are stored)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso_utc(moment: datetime) -> str:
    """An instant for the wire, always with its offset.

    An offset-less string is read by a browser as the viewer's local time, five hours off in
    Almaty — the preview must never hand one out.
    """
    return _as_utc(moment).isoformat()


def _span_minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _plural_ru(value: int, forms: tuple[str, str, str]) -> str:
    """«урок / урока / уроков» for ``value`` — the CRM's ``period_units._plural_ru``."""
    n = abs(value)
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    if 2 <= n % 10 <= 4:
        return forms[1]
    return forms[2]


def duplicate_lesson_rows(db: Session, group_id: int) -> int:
    """Extra active class lessons sharing a start minute, past and future alike.

    Rows at duplicated instants minus the number of such instants. The LMS save does not merge
    these, so each extra row is counted — a taught duplicate as a lesson taught, a future one as
    a lesson to pair — and the preview has to say so.
    """
    from src.events.models import Event, EventGroup

    per_instant: dict[datetime, int] = {}
    rows = (
        db.query(Event.start_datetime)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id == group_id,
            Event.event_type == "class",
            Event.is_active == True,  # noqa: E712
        )
        .all()
    )
    for (start,) in rows:
        if start is None:
            continue
        key = _as_utc(start).replace(second=0, microsecond=0)
        per_instant[key] = per_instant.get(key, 0) + 1
    return sum(count - 1 for count in per_instant.values() if count > 1)


def preview_group_schedule(
    db: Session,
    group_id: int,
    config: Any,
    *,
    previous_config: Any,
    fallback_start: Optional[date],
    now: Optional[datetime] = None,
) -> dict:
    """What :func:`apply_group_schedule` would do with ``config`` — computed, never written."""
    now_utc = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    state = load_schedule_state(db, group_id, now_utc)
    slots = desired_schedule_slots(config, state, now_utc, fallback_start)
    changes = plan_schedule_changes(
        [LessonSpan(e.id, e.start_datetime, e.end_datetime) for e in state.future_events],
        slots,
        weekly_slot_minutes(previous_config),
    )
    kept = [c for c in changes if c.kind != "deactivate"]
    planned_minutes = sum(_span_minutes(c.start, c.end) for c in kept)
    warnings: list[str] = []
    if is_counted_schedule(config) and int(config["lessons_count"]) <= state.started_count:
        warnings.append(
            f"Кол-во уроков ({int(config['lessons_count'])}) не больше, чем уже прошло "
            f"({state.started_count}) — новых уроков не будет"
        )
    # A start date ahead of today reads like «a new cycle from then», but the lessons already
    # taught still count towards «Кол-во уроков» — a reused group would plan fewer than meant.
    start = parse_start_date(config, None)
    if state.started_count > 0 and start and start > now_utc.astimezone(KZ_TZ).date():
        lessons_word = _plural_ru(state.started_count, ("урок", "урока", "уроков"))
        warnings.append(
            f"Дата начала позже сегодняшней, но у группы уже прошло {state.started_count} "
            f"{lessons_word} — они засчитаны в «Кол-во уроков»; для нового цикла создайте "
            "новую группу или увеличьте количество."
        )
    # Unlike the CRM's, the LMS save does not merge duplicates first: the numbers above already
    # count them exactly as the save will, and the warning says the save will not tidy them up.
    extra_rows = duplicate_lesson_rows(db, group_id)
    if extra_rows:
        warnings.append(
            f"Есть дубли уроков ({extra_rows}) — сохранение их не объединит, итог посчитан с ними."
        )
    return {
        "started_lessons": state.started_count,
        "started_minutes": state.started_minutes,
        "planned_lessons": len(kept),
        "planned_minutes": planned_minutes,
        "total_lessons": state.started_count + len(kept),
        "total_minutes": state.started_minutes + planned_minutes,
        "first_start": _iso_utc(kept[0].start) if kept else None,
        "last_end": _iso_utc(kept[-1].end) if kept else None,
        "lessons": [
            {
                "start": _iso_utc(c.start),
                "end": _iso_utc(c.end),
                "minutes": _span_minutes(c.start, c.end),
                "event_id": c.event_id,
                "change": c.kind,
                "previous_start": _iso_utc(c.previous_start) if c.previous_start else None,
                "previous_end": _iso_utc(c.previous_end) if c.previous_end else None,
            }
            for c in kept
        ],
        "deactivated": [
            {"event_id": c.event_id, "start": _iso_utc(c.start), "end": _iso_utc(c.end)}
            for c in changes
            if c.kind == "deactivate"
        ],
        "warnings": warnings,
    }
