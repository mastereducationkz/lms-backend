"""iCalendar (RFC 5545) text for the LMS calendar feeds, built by hand.

The feeds carry three kinds of entries — timed lessons, timed weekly tests and all-day homework
deadlines — with a title, a description and a link. That is a few dozen lines of the standard;
a dependency (``icalendar``) would add a package to the image for less than this file, and the
parts that bite in practice are exactly the ones spelled out here: CRLF line endings, 75-octet
line folding that never splits a UTF-8 character (every title here is Cyrillic), and TEXT
escaping. Everything is emitted in UTC (``…Z``), so no VTIMEZONE block is needed; all-day
deadlines are floating DATE values on their Almaty calendar day.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

PRODID = "-//Master Education//LMS Calendar//RU"
REFRESH = "PT15M"


@dataclass(frozen=True)
class CalendarItem:
    """One entry of a calendar, shared by the ICS feeds and the Google Calendar sync.

    ``key`` is the stable identity (``lesson-123``, ``weekly-456``, ``deadline-789``): the ICS UID
    is built from it and so is the Google event id, which is what makes both idempotent.
    Timed entries carry naive-UTC ``start``/``end`` (as stored); an all-day entry carries ``day``.
    """

    key: str
    summary: str
    description: str = ""
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    day: Optional[date] = None
    url: Optional[str] = None
    updated: Optional[datetime] = None

    @property
    def all_day(self) -> bool:
        return self.day is not None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)


def _stamp(value: datetime) -> str:
    return f"{_utc(value):%Y%m%dT%H%M%SZ}"


def escape_text(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n"))


def fold(line: str) -> str:
    """Fold a content line at 75 octets, never inside a multi-byte UTF-8 character."""
    out, current, size = [], [], 0
    limit = 75
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            out.append("".join(current))
            current, size, limit = [], 0, 74          # continuation lines start with a space
        current.append(char)
        size += width
    out.append("".join(current))
    return "\r\n ".join(out)


def uid(key: str) -> str:
    return f"{key}@lms.mastereducation.kz"


def sequence(updated: Optional[datetime]) -> int:
    """Grows whenever the entry changes: minutes since 2020, well inside a 32-bit int."""
    if updated is None:
        return 0
    return max(0, int((_utc(updated) - datetime(2020, 1, 1)).total_seconds() // 60))


def _event_lines(item: CalendarItem, now: datetime) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{uid(item.key)}", f"DTSTAMP:{_stamp(now)}",
             f"SEQUENCE:{sequence(item.updated)}"]
    if item.all_day:
        lines.append(f"DTSTART;VALUE=DATE:{item.day:%Y%m%d}")
        lines.append(f"DTEND;VALUE=DATE:{item.day + timedelta(days=1):%Y%m%d}")
        lines.append("TRANSP:TRANSPARENT")
    else:
        lines.append(f"DTSTART:{_stamp(item.start)}")
        lines.append(f"DTEND:{_stamp(item.end or item.start + timedelta(hours=1))}")
    lines.append(f"SUMMARY:{escape_text(item.summary)}")
    if item.description:
        lines.append(f"DESCRIPTION:{escape_text(item.description)}")
    if item.url:
        lines.append(f"URL:{item.url}")
    if item.updated is not None:
        lines.append(f"LAST-MODIFIED:{_stamp(item.updated)}")
    lines.append("END:VEVENT")
    return lines


def build(name: str, items: Iterable[CalendarItem], now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{escape_text(name)}", "X-WR-TIMEZONE:Asia/Almaty",
             f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH}", f"X-PUBLISHED-TTL:{REFRESH}"]
    for item in sorted(items, key=lambda i: (i.day or (i.start.date() if i.start else date.min), i.key)):
        lines.extend(_event_lines(item, now))
    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)
