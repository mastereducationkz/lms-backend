"""Project stored platform events into ``platform_results`` / ``platform_weekly_sets``
(Platform Integration Pack §2.3 + §2.5). Idempotent: applying the same event twice is a no-op,
and a result's status only ever moves forward."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from src.integrations.models import PlatformEvent, PlatformResult, PlatformWeeklySet
from src.integrations.schemas import to_naive_utc


class ProjectionDataError(ValueError):
    """The event type is known but its ``data`` does not carry what the catalogue promises."""


class UnhandledEventType(Exception):
    """Not in the v1 catalogue: stored for later, never projected, never rejected."""


_ATTEMPT_STATUS = {
    "attempt.started": "started",
    "attempt.submitted": "submitted",
    "attempt.expired": "expired",
    "writing.completed": "completed",
    "speaking.completed": "completed",
}
STATUS_RANK = {"started": 1, "submitted": 2, "expired": 2, "completed": 2, "scored": 3}

HANDLED_EVENT_TYPES = frozenset(
    {"weekly_set.published", "weekly_set.updated", "weekly_set.unpublished", "result.ready"}
    | set(_ATTEMPT_STATUS)
)


def apply_event(db: Session, event: PlatformEvent) -> None:
    """Apply one stored event. Raises UnhandledEventType / ProjectionDataError; the caller
    decides what those mean for the sender."""
    data = (event.payload or {}).get("data") or {}
    event_type = event.event_type
    if event_type in ("weekly_set.published", "weekly_set.updated"):
        _sync_assignments(db, _upsert_weekly_set(db, event.platform, data))
    elif event_type == "weekly_set.unpublished":
        _sync_assignments(db, _upsert_weekly_set(db, event.platform, {**data, "is_active": False}, create=False))
    elif event_type in _ATTEMPT_STATUS:
        ref_key = "session_id" if event_type == "writing.completed" else "attempt_id"
        _upsert_result(
            db, event, data, status=_ATTEMPT_STATUS[event_type], attempt_ref=_required(data, ref_key)
        )
    elif event_type == "result.ready":
        attempt_ref = data.get("attempt_ref")
        if attempt_ref in (None, ""):
            attempt_ref = _legacy_attempt_ref(event, data)
        _upsert_result(db, event, data, status="scored", attempt_ref=attempt_ref)
    else:
        raise UnhandledEventType(event_type)


# --- weekly sets ---------------------------------------------------------------------

def _sync_assignments(db: Session, ws) -> None:
    """E1: keep the per-group platform_test assignments in step with the set (flag-gated)."""
    if ws is None:
        return
    from src.integrations import platform_assignments

    if platform_assignments.enabled():
        platform_assignments.sync_weekly_set(db, ws)


def _upsert_weekly_set(db: Session, platform: str, data: dict, *, create: bool = True):
    set_id = _int(_required(data, "weekly_set_id"), "weekly_set_id")
    row = (
        db.query(PlatformWeeklySet)
        .filter(PlatformWeeklySet.platform == platform, PlatformWeeklySet.weekly_set_id == set_id)
        .first()
    )
    if row is None:
        if not create:
            return None  # unpublishing a set we never saw: nothing to deactivate
        row = PlatformWeeklySet(platform=platform, weekly_set_id=set_id)
        db.add(row)
    if "title" in data:
        row.title = data.get("title")
    # Full timestamps (a date-only value means midnight UTC); Speaking closes at date_to's minute.
    if "date_from" in data:
        row.date_from = _parse_dt(data["date_from"], "date_from") if data.get("date_from") else None
    if "date_to" in data:
        row.date_to = _parse_dt(data["date_to"], "date_to") if data.get("date_to") else None
    if "is_active" in data:
        row.is_active = bool(data.get("is_active"))
    if "track" in data:
        row.track = data.get("track")
    if "set_path" in data:
        row.set_path = str(data["set_path"]) if data.get("set_path") else None
    if "modules" in data:
        modules = data.get("modules")
        if modules is not None and not isinstance(modules, list):
            raise ProjectionDataError("modules must be a list")
        row.modules = modules
    db.flush()
    return row


def _legacy_attempt_ref(event: PlatformEvent, data: dict) -> str:
    """SAT legacy results have no attempt: key them per test *per student*, from the student's
    platform identity (subject, else e-mail) so two students on the same test never share a row
    — the unique key is (platform, module, attempt_ref), and the sender may not be resolved yet."""
    if data.get("test_id") in (None, ""):
        raise ProjectionDataError("attempt_ref (or test_id) is required")
    if event.zitadel_subject:
        ident = str(event.zitadel_subject).strip()
    elif event.email:
        ident = hashlib.sha1(event.email.strip().lower().encode()).hexdigest()[:16]
    else:
        raise ProjectionDataError("result.ready without attempt_ref needs a student identity")
    return f"test:{_int(data['test_id'], 'test_id')}:{ident}"[:64]


# --- results -------------------------------------------------------------------------

def _upsert_result(db: Session, event: PlatformEvent, data: dict, *, status: str, attempt_ref: Any) -> None:
    module = str(_required(data, "module"))
    attempt_ref = str(attempt_ref)
    row = (
        db.query(PlatformResult)
        .filter(
            PlatformResult.platform == event.platform,
            PlatformResult.module == module,
            PlatformResult.attempt_ref == attempt_ref,
        )
        .first()
    )
    if row is None:
        row = PlatformResult(
            platform=event.platform,
            module=module,
            attempt_ref=attempt_ref,
            status=status,
            track=str(data.get("track") or event.platform),
        )
        db.add(row)
    if row.user_id is None and event.user_id is not None:
        row.user_id = event.user_id

    # Descriptive fields fill in from any event that carries them (never blanked).
    for key in ("test_id", "weekly_set_id"):
        if data.get(key) is not None:
            setattr(row, key, _int(data[key], key))
    if data.get("test_title"):
        row.test_title = str(data["test_title"])
    if data.get("result_url"):
        row.result_url = str(data["result_url"])
    for key in ("started_at", "finished_at"):
        if data.get(key):
            setattr(row, key, _parse_dt(data[key], key))

    # Status is monotonic; a late attempt.* after result.ready must not downgrade a score.
    if STATUS_RANK[status] >= STATUS_RANK.get(row.status, 0):
        row.status = status
    if status == "scored":
        if "band" in data:
            row.band = _float(data.get("band"), "band")
        if "raw_score" in data:
            row.raw_score = _int(data.get("raw_score"), "raw_score", allow_none=True)
        if "total" in data:
            row.total = _int(data.get("total"), "total", allow_none=True)
        if data.get("scored_at"):
            row.scored_at = _parse_dt(data["scored_at"], "scored_at")
        elif row.scored_at is None:
            row.scored_at = event.occurred_at
    db.flush()

    # E1: a module just changed state — write the auto submission if the set is now complete.
    from src.integrations import platform_progress

    platform_progress.on_result_change(db, event.platform, row.weekly_set_id, row.user_id)


# --- field helpers -------------------------------------------------------------------

def _required(data: dict, key: str) -> Any:
    value = data.get(key)
    if value is None or value == "":
        raise ProjectionDataError(f"{key} is required")
    return value


def _int(value: Any, key: str, *, allow_none: bool = False) -> Optional[int]:
    if value is None:
        if allow_none:
            return None
        raise ProjectionDataError(f"{key} is required")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ProjectionDataError(f"{key} must be an integer") from exc


def _float(value: Any, key: str) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ProjectionDataError(f"{key} must be a number") from exc


def _parse_dt(value: Any, key: str) -> datetime:
    if isinstance(value, datetime):
        return to_naive_utc(value)
    try:
        return to_naive_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError as exc:
        raise ProjectionDataError(f"{key} must be an ISO-8601 timestamp") from exc


def _parse_date(value: Any, key: str) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ProjectionDataError(f"{key} must be an ISO date") from exc
