from fastapi import APIRouter, Depends, HTTPException, Query, Security
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy.orm import Session
from typing import List, Optional, Literal
from datetime import datetime, date, timedelta, timezone
import os
import calendar

from src.config import get_db
from src.schemas.models import Group, Event, EventGroup, UserInDB

router = APIRouter()

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def verify_masteredu_api_key(api_key: str = Security(api_key_header)) -> str:
    expected = os.getenv("MASTEREDU_API_KEY")
    if not expected or api_key != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing API key")
    return api_key


FilterType = Literal["all", "past", "upcoming", "today", "this_week", "this_month"]


def _get_date_range(
    filter_type: FilterType,
    date_filter: Optional[date],
    week_date: Optional[date],
    month: Optional[int],
    year: Optional[int],
):
    """
    Resolve (start, end) UTC-naive datetime bounds.

    Priority:
      1. date_filter  — exact calendar day
      2. week_date    — the ISO week that contains this date
      3. month+year   — a specific calendar month
      4. filter_type  — predefined relative filters
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # --- Specific date ---
    if date_filter is not None:
        day_start = datetime(date_filter.year, date_filter.month, date_filter.day)
        return day_start, day_start + timedelta(days=1)

    # --- Specific week (by any date inside that ISO week) ---
    if week_date is not None:
        week_start = datetime(week_date.year, week_date.month, week_date.day) - timedelta(days=week_date.weekday())
        return week_start, week_start + timedelta(days=7)

    # --- Specific month (month + year required together) ---
    if month is not None or year is not None:
        if month is None or year is None:
            raise HTTPException(
                status_code=422,
                detail="Both 'month' (1–12) and 'year' must be provided together",
            )
        if not (1 <= month <= 12):
            raise HTTPException(status_code=422, detail="'month' must be between 1 and 12")
        month_start = datetime(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        month_end = datetime(year, month, last_day) + timedelta(days=1)
        return month_start, month_end

    # --- Predefined relative filters ---
    if filter_type == "all":
        return None, None
    if filter_type == "past":
        return None, now
    if filter_type == "upcoming":
        return now, None
    if filter_type == "today":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return today_start, today_start + timedelta(days=1)
    if filter_type == "this_week":
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return week_start, week_start + timedelta(days=7)
    if filter_type == "this_month":
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_day = calendar.monthrange(now.year, now.month)[1]
        month_end = datetime(now.year, now.month, last_day) + timedelta(days=1)
        return month_start, month_end

    return None, None


@router.get("/schedules")
def get_sat_group_schedules(
    filter: FilterType = Query("all", description="Predefined filter: all | past | upcoming | today | this_week | this_month"),
    date_filter: Optional[date] = Query(None, alias="date", description="Specific date (YYYY-MM-DD). Overrides 'filter'."),
    week_date: Optional[date] = Query(None, description="Any date inside the target week (YYYY-MM-DD). Overrides 'filter'."),
    month: Optional[int] = Query(None, ge=1, le=12, description="Month number (1–12). Must be used together with 'year'."),
    year: Optional[int] = Query(None, ge=2000, le=2100, description="4-digit year. Must be used together with 'month'."),
    group_id: Optional[int] = Query(None, description="Filter by a specific SAT group ID"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
    _: str = Depends(verify_masteredu_api_key),
):
    """
    Return lesson schedules (Events) for all SAT groups.

    **Authentication:** `X-API-Key` header (value from `MASTEREDU_API_KEY`).

    **Date selection priority** (first match wins):
    1. `date=YYYY-MM-DD`        — a single specific day
    2. `week_date=YYYY-MM-DD`   — the full ISO week containing that day
    3. `month=M&year=YYYY`      — a specific calendar month
    4. `filter`                 — predefined relative keyword (all / past / upcoming / today / this_week / this_month)
    """
    date_start, date_end = _get_date_range(filter, date_filter, week_date, month, year)

    sat_groups_query = (
        db.query(Group)
        .filter(Group.program_type == "sat")
    )
    if group_id is not None:
        sat_groups_query = sat_groups_query.filter(Group.id == group_id)

    sat_groups: List[Group] = sat_groups_query.all()
    sat_group_ids = [g.id for g in sat_groups]

    if not sat_group_ids:
        return {
            "filter": filter,
            "total_groups": 0,
            "total_events": 0,
            "groups": [],
        }

    # Build event query joined through EventGroup
    events_query = (
        db.query(Event)
        .join(EventGroup, EventGroup.event_id == Event.id)
        .filter(
            EventGroup.group_id.in_(sat_group_ids),
            Event.is_active == True,
        )
    )

    if date_start is not None:
        events_query = events_query.filter(Event.start_datetime >= date_start)
    if date_end is not None:
        events_query = events_query.filter(Event.start_datetime < date_end)

    events_query = events_query.order_by(Event.start_datetime)
    all_events: List[Event] = events_query.offset(skip).limit(limit).all()

    # Map events back to groups
    group_map = {g.id: g for g in sat_groups}
    group_events: dict[int, list] = {g.id: [] for g in sat_groups}

    for event in all_events:
        for eg in event.event_groups:
            if eg.group_id in group_events:
                group_events[eg.group_id].append(event)

    teacher_ids = {g.teacher_id for g in sat_groups if g.teacher_id}
    teachers: dict[int, UserInDB] = {}
    if teacher_ids:
        teacher_rows = db.query(UserInDB).filter(UserInDB.id.in_(teacher_ids)).all()
        teachers = {t.id: t for t in teacher_rows}

    groups_output = []
    for group in sat_groups:
        teacher = teachers.get(group.teacher_id) if group.teacher_id else None
        events_for_group = group_events.get(group.id, [])

        groups_output.append({
            "group_id": group.id,
            "group_name": group.name,
            "is_active": group.is_active,
            "is_over": group.is_over,
            "teacher_id": group.teacher_id,
            "teacher_name": teacher.name if teacher else None,
            "events_count": len(events_for_group),
            "events": [
                {
                    "id": ev.id,
                    "title": ev.title,
                    "event_type": ev.event_type,
                    "start_datetime": ev.start_datetime.isoformat() + "Z",
                    "end_datetime": ev.end_datetime.isoformat() + "Z",
                    "location": ev.location,
                    "is_online": ev.is_online,
                    "meeting_url": ev.meeting_url,
                    "teacher_id": ev.teacher_id,
                    "teacher_name": ev.teacher.name if ev.teacher else None,
                    "is_substitution": ev.is_substitution,
                    "is_recurring": ev.is_recurring,
                    "recurrence_pattern": ev.recurrence_pattern,
                }
                for ev in events_for_group
            ],
        })

    return {
        "filter": filter,
        "total_groups": len(groups_output),
        "total_events": sum(g["events_count"] for g in groups_output),
        "groups": groups_output,
    }
