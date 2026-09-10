"""How API datetimes are written: UTC, with a Z, and never both an offset and a Z.

The schemas used ``lambda v: v.isoformat() + "Z"``, which is right only for a naive datetime.
Cached responses (``cache_service.cached``) are stored as JSON and, on a hit, re-validated
into the response model — the "…Z" string comes back as an *aware* datetime, and the lambda
then wrote "2026-08-01T05:00:00+00:00Z", which no browser can parse. So every reload inside the
cache TTL (30 s on the calendar) quietly lost its events; found 2026-09-10 when a stricter
calendar helper turned that into a crash.
"""
from datetime import datetime, timezone
from typing import Optional


def utc_z(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"
