"""How a group's weekly pattern becomes lessons — the rules, with no database.

Two questions, answered once so a schedule save, its preview and the group card cannot
disagree:

* :func:`future_schedule_slots` — the future lessons a counted course still needs.
* :func:`plan_schedule_changes` — which existing lesson goes where, and how long it runs.

«Кол-во уроков» counts the whole course, taught lessons included. The old rule replayed the
*new* pattern from ``start_date`` and kept whatever fell in the future, so changing the pattern
mid-course scheduled the wrong number: «Indi Rauan IELTS 2026» (14 taught, Mon/Wed/Fri/Sat/Sun
→ Mon/Fri + Sat/Sun) would have received 24 more lessons instead of 18, because the pattern's
twelve imaginary past slots are not the fourteen lessons actually taught.

This is the LMS mirror of the CRM's ``crm-master/backend/src/groups/schedule_plan.py``; it
lives in ``lms-backend/src/services/schedule_plan.py``. The two must agree. The CRM helpers it
needs (``src.groups.helpers``) are copied below with identical behaviour; ``parse_start_date``
is public, as in the CRM, because the schedule preview reads it too.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Literal, Optional, Sequence

#: Almaty, a fixed UTC+5 — twin of the CRM's ``src.core.business_time.KZ_TZ``.
KZ_TZ = timezone(timedelta(hours=5))

#: A pattern whose every slot is excluded must not loop for ever.
MAX_WEEKS = 520

ChangeKind = Literal["keep", "move", "resize", "create", "deactivate"]

#: How long a lesson runs when the slot does not say. Every schedule written before durations
#: existed means this, so reading it as the default keeps old groups generating exactly the
#: events they generated before.
DEFAULT_SLOT_MINUTES = 60

#: Bounds for a stored slot duration. Wider than the offered set, because a 45- or 75-minute
#: group is a real thing; narrow enough that a typo cannot generate a 20-hour lesson.
MIN_SLOT_MINUTES = 15
MAX_SLOT_MINUTES = 300


# ------------------------------------------------------------------ CRM `groups/helpers.py`


def parse_start_date(schedule_config: Any, created_at: Optional[datetime]) -> Optional[date]:
    if isinstance(schedule_config, dict):
        raw = schedule_config.get("start_date")
        if raw:
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                pass
    if created_at:
        if isinstance(created_at, datetime):
            return created_at.date()
        return created_at
    return None


def _to_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _slot_key_kz(dt: datetime) -> str:
    """Match key for schedule slot and event (date + time in Kazakhstan)."""
    kz = _to_utc_aware(dt).astimezone(KZ_TZ)
    return f"{kz.date().isoformat()}_{kz.strftime('%H:%M')}"


def _slot_duration_minutes(item: Any) -> int:
    """How long this schedule slot's lessons run, in minutes.

    Per slot rather than per group: a group that meets for an hour on Monday and ninety
    minutes on Saturday is ordinary, and a single group-level number could not say so.
    Anything missing, unparseable or out of range reads as the 60-minute default, so a
    malformed value degrades to the old behaviour instead of generating a nonsense lesson.
    """
    if not isinstance(item, dict):
        return DEFAULT_SLOT_MINUTES
    raw = item.get("duration_minutes", item.get("duration"))
    if raw is None:
        return DEFAULT_SLOT_MINUTES
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SLOT_MINUTES
    if minutes < MIN_SLOT_MINUTES or minutes > MAX_SLOT_MINUTES:
        return DEFAULT_SLOT_MINUTES
    return minutes


def _excluded_slot_keys(schedule_config: Any) -> set[str]:
    if not isinstance(schedule_config, dict):
        return set()
    raw = schedule_config.get("excluded_slot_keys") or []
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if item}


def _expand_schedule_occurrences(
    schedule_config: Any,
    *,
    fallback_start: Optional[date] = None,
) -> list[tuple[datetime, int]]:
    """Every generated lesson as ``(start UTC-aware, duration in minutes)`` — weeks-based."""
    if not isinstance(schedule_config, dict):
        return []
    items = schedule_config.get("schedule_items") or []
    if not items:
        return []

    start = parse_start_date(schedule_config, None) or fallback_start
    if not start:
        return []

    lessons_count = schedule_config.get("lessons_count")
    weeks_count = schedule_config.get("weeks_count")
    frequency = len(items)
    excluded_keys = _excluded_slot_keys(schedule_config)
    if weeks_count is None:
        if lessons_count:
            weeks_count = math.ceil(int(lessons_count) / frequency) + 2
        else:
            weeks_count = 12

    all_dates: list[tuple[datetime, int]] = []
    for week in range(int(weeks_count)):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                day_index = int(item.get("day_of_week"))
            except (TypeError, ValueError):
                continue
            if day_index < 0 or day_index > 6:
                continue
            time_raw = item.get("time_of_day") or item.get("time") or "19:00"
            time_str = str(time_raw).strip()[:5]
            try:
                time_obj = datetime.strptime(time_str, "%H:%M").time()
            except ValueError:
                time_obj = time(19, 0)

            days_ahead = day_index - start.weekday()
            if days_ahead < 0:
                days_ahead += 7
            target_date = start + timedelta(days=days_ahead) + timedelta(weeks=week)
            if target_date < start:
                continue
            target_dt_kz = datetime.combine(target_date, time_obj)
            target_dt_utc = target_dt_kz.replace(tzinfo=KZ_TZ).astimezone(timezone.utc)
            if _slot_key_kz(target_dt_utc) in excluded_keys:
                continue
            all_dates.append((target_dt_utc, _slot_duration_minutes(item)))

    all_dates.sort(key=lambda pair: pair[0])
    if lessons_count:
        all_dates = all_dates[: int(lessons_count)]
    return all_dates


# ------------------------------------------------------------------ the rules


def _utc(dt: datetime) -> datetime:
    """Naive values are UTC (how events are stored); minutes are the unit of a schedule."""
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return aware.replace(second=0, microsecond=0)


def _pattern(config: Any) -> list[tuple[int, time, int]]:
    """``(weekday, start, minutes)`` for every valid schedule item, as generation reads them."""
    if not isinstance(config, dict):
        return []
    out: list[tuple[int, time, int]] = []
    for item in config.get("schedule_items") or []:
        if not isinstance(item, dict):
            continue
        try:
            weekday = int(item.get("day_of_week"))
        except (TypeError, ValueError):
            continue
        if weekday < 0 or weekday > 6:
            continue
        raw = str(item.get("time_of_day") or item.get("time") or "19:00").strip()[:5]
        try:
            at = datetime.strptime(raw, "%H:%M").time()
        except ValueError:
            at = time(19, 0)
        out.append((weekday, at, _slot_duration_minutes(item)))
    return out


def weekly_slot_minutes(config: Any) -> dict[tuple[int, str], int]:
    """``(weekday, "HH:MM") → minutes`` — what a save compares to tell a changed day.

    Two items for the same day and time should not happen, but if they do, the first one
    wins here too — matching :func:`future_schedule_slots`, whose ``seen`` set already keeps
    the first occurrence of a slot's exact instant and drops the rest.
    """
    out: dict[tuple[int, str], int] = {}
    for weekday, at, minutes in _pattern(config):
        out.setdefault((weekday, at.strftime("%H:%M")), minutes)
    return out


def is_counted_schedule(config: Any) -> bool:
    if not isinstance(config, dict):
        return False
    raw = config.get("lessons_count")
    if isinstance(raw, bool):
        return False
    try:
        return int(raw) > 0
    except (TypeError, ValueError):
        return False


def future_schedule_slots(
    config: Any,
    *,
    started: int,
    now: datetime,
    skip_instants: Iterable[datetime] = (),
    fallback_start: Optional[date] = None,
) -> list[tuple[datetime, int]]:
    """The future lessons the course still needs, as ``(start UTC-aware, minutes)``.

    ``remaining = lessons_count − started``, placed on the pattern from the later of the start
    date and today (Almaty), at instants ``>= now``. Excluded slot keys and ``skip_instants``
    (approved cancellations) are passed over without being counted. A schedule with no
    ``lessons_count`` keeps the weeks-based expansion it always had, less the instants in
    ``skip_instants``.
    """
    # `now` is compared against slot instants at second precision, not truncated to the minute
    # like a slot or event start: truncating it would let an instant a few seconds in the past
    # (naive) or a few seconds in the future (aware) disagree about whether a slot still counts.
    now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    # Approved cancellations bind both branches. The LMS only switches the lesson off (and
    # lowers a count when there is one), so without a count nothing else remembers it and the
    # weeks-based expansion would put the lesson straight back.
    skip = {_utc(instant) for instant in skip_instants}
    if not is_counted_schedule(config):
        return [
            (dt, minutes)
            for dt, minutes in _expand_schedule_occurrences(config, fallback_start=fallback_start)
            if dt >= now_utc and _utc(dt) not in skip
        ]
    pattern = _pattern(config)
    if not pattern:
        return []
    remaining = int(config["lessons_count"]) - max(0, int(started))
    if remaining <= 0:
        return []

    start = parse_start_date(config, None) or fallback_start
    today = now_utc.astimezone(KZ_TZ).date()
    anchor = max(start, today) if start else today
    excluded = _excluded_slot_keys(config)

    out: list[tuple[datetime, int]] = []
    seen: set[datetime] = set()
    for week in range(MAX_WEEKS):
        week_slots: list[tuple[datetime, int]] = []
        for weekday, at, minutes in pattern:
            day = anchor + timedelta(days=(weekday - anchor.weekday()) % 7, weeks=week)
            instant = datetime.combine(day, at).replace(tzinfo=KZ_TZ).astimezone(timezone.utc)
            if instant < now_utc or instant in seen or instant in skip:
                continue
            if _slot_key_kz(instant) in excluded:
                continue
            seen.add(instant)
            week_slots.append((instant, minutes))
        for slot in sorted(week_slots, key=lambda pair: pair[0]):
            out.append(slot)
            if len(out) >= remaining:
                return out
    return out


@dataclass(frozen=True)
class LessonSpan:
    event_id: int
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PlannedChange:
    kind: ChangeKind
    event_id: Optional[int]
    start: datetime
    end: datetime
    previous_start: Optional[datetime] = None
    previous_end: Optional[datetime] = None


def _keeps_own_length(
    start: datetime, minutes: int, previous_minutes: Optional[dict[tuple[int, str], int]]
) -> bool:
    """A lesson that stays put keeps its length unless its day's length changed in this save.

    Without the comparison every save rewrote every future end time from its slot, so a
    lesson shortened by hand (Rauan's last Saturday) silently grew back. ``None`` means the
    caller cannot say what the day was before — the slot decides, as it always did.
    """
    if previous_minutes is None:
        return False
    local = start.astimezone(KZ_TZ)
    return previous_minutes.get((local.weekday(), local.strftime("%H:%M"))) == minutes


def plan_schedule_changes(
    future_events: Sequence[LessonSpan],
    desired: Sequence[tuple[datetime, int]],
    previous_minutes: Optional[dict[tuple[int, str], int]],
) -> list[PlannedChange]:
    """Pair the group's future lessons with the desired slots.

    1. A lesson starting exactly on a slot keeps it.
    2. The rest move, in date order, onto the remaining slots in date order — the id, its
       attendance, Meet room and any substitution travel with it.
    3. Leftover slots are created; leftover lessons are switched off.

    Pure positional pairing (the old rule) moved nearly every lesson of a group whose pattern
    changed by one day, carrying a substitution pinned to Friday's lesson onto Saturday.
    """
    events = sorted(future_events, key=lambda e: (_utc(e.start), e.event_id))
    slots = sorted(((_utc(dt), int(minutes)) for dt, minutes in desired), key=lambda s: s[0])

    waiting: dict[datetime, list[LessonSpan]] = {}
    for event in events:
        waiting.setdefault(_utc(event.start), []).append(event)

    assigned: dict[int, LessonSpan] = {}
    used: set[int] = set()
    for index, (start, _minutes) in enumerate(slots):
        bucket = waiting.get(start)
        if bucket:
            event = bucket.pop(0)
            assigned[index] = event
            used.add(event.event_id)

    free_events = [e for e in events if e.event_id not in used]
    free_slots = [i for i in range(len(slots)) if i not in assigned]
    for event, index in zip(free_events, free_slots):
        assigned[index] = event
        used.add(event.event_id)

    changes: list[PlannedChange] = []
    for index, (start, minutes) in enumerate(slots):
        slot_end = start + timedelta(minutes=minutes)
        event = assigned.get(index)
        if event is None:
            changes.append(PlannedChange("create", None, start, slot_end))
            continue
        was_start, was_end = _utc(event.start), _utc(event.end)
        if was_start != start:
            changes.append(PlannedChange("move", event.event_id, start, slot_end, was_start, was_end))
        elif _keeps_own_length(start, minutes, previous_minutes) or was_end == slot_end:
            changes.append(PlannedChange("keep", event.event_id, was_start, was_end, was_start, was_end))
        else:
            changes.append(PlannedChange("resize", event.event_id, start, slot_end, was_start, was_end))

    for event in events:
        if event.event_id not in used:
            was_start, was_end = _utc(event.start), _utc(event.end)
            changes.append(
                PlannedChange("deactivate", event.event_id, was_start, was_end, was_start, was_end)
            )
    return changes
