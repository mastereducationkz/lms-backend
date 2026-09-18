"""Границы отчётной недели.

Неделя куратора — понедельник … воскресенье по Алматы. В базе ``Event.start_datetime``
лежит наивным UTC, поэтому окно нужно не просто построить, но и сдвинуть: понедельник
00:00 в Алматы — это воскресенье 19:00 UTC. Сравнение локальной даты с наивным UTC
напрямую увозит границу на пять часов и молча переносит вечерние воскресные занятия
в следующий отчёт.

Алматы — UTC+5 круглый год, переходов на летнее время нет с 2005 года, поэтому
фиксированный offset здесь корректен и не требует tzdata.
"""
from datetime import date, datetime, time, timedelta, timezone

ALMATY = timezone(timedelta(hours=5))


def week_bounds(day: date) -> tuple[date, date]:
    """(понедельник, воскресенье) недели, в которую попадает ``day``."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def week_utc_range(week_start: date) -> tuple[datetime, datetime]:
    """Полуинтервал ``[start, end)`` в наивном UTC для алматинской недели.

    Полуинтервал, а не включающий конец: так воскресенье 23:59:59.999 попадает внутрь
    без возни с микросекундами на границе.
    """
    start_local = datetime.combine(week_start, time(0, 0), tzinfo=ALMATY)
    end_local = start_local + timedelta(days=7)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None),
        end_local.astimezone(timezone.utc).replace(tzinfo=None),
    )
