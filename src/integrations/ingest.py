"""Ingest a batch of platform events (Platform Integration Pack §2.1).

Per-event outcomes, never a batch failure:
  accepted   — stored (and projected when the type is in the v1 catalogue)
  duplicates — ``(platform, event_id)`` already stored; acknowledged, never re-applied
  rejected   — schema error (envelope or the type's required ``data`` fields); the sender
               dead-letters that one row and moves on
Each event is its own transaction so a rejected or duplicate event can never roll back an
accepted sibling.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.integrations.models import PlatformEvent
from src.integrations.projection import ProjectionDataError, UnhandledEventType, apply_event
from src.integrations.resolver import resolve_user_id
from src.integrations.schemas import Envelope, validation_reason

logger = logging.getLogger(__name__)

ERROR_UNRESOLVED = "unresolved"
ERROR_UNHANDLED = "unhandled_event_type"


def ingest_batch(db: Session, platform: str, events: list[Any]) -> dict:
    accepted: list[str] = []
    duplicates: list[str] = []
    rejected: list[dict] = []

    parsed: list[Envelope] = []
    for raw in events:
        raw_id = raw.get("event_id") if isinstance(raw, dict) else None
        try:
            env = Envelope.model_validate(raw)
        except ValidationError as exc:
            rejected.append({"event_id": raw_id, "reason": validation_reason(exc)})
            continue
        if env.platform != platform:
            rejected.append({"event_id": env.event_id, "reason": "platform: does not match X-Platform"})
            continue
        parsed.append(env)

    # Apply in occurred_at order so a batch that arrives out of order still projects correctly.
    parsed.sort(key=lambda e: e.occurred_at)
    for env in parsed:
        outcome, reason = _ingest_one(db, platform, env)
        if outcome == "accepted":
            accepted.append(env.event_id)
        elif outcome == "duplicate":
            duplicates.append(env.event_id)
        else:
            rejected.append({"event_id": env.event_id, "reason": reason})
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


def _ingest_one(db: Session, platform: str, env: Envelope) -> tuple[str, str | None]:
    exists = (
        db.query(PlatformEvent.id)
        .filter(PlatformEvent.platform == platform, PlatformEvent.event_id == env.event_id)
        .first()
    )
    if exists:
        return "duplicate", None

    student = env.student
    event = PlatformEvent(
        platform=platform,
        event_id=env.event_id,
        event_type=env.event_type,
        occurred_at=env.occurred_at,
        received_at=datetime.now(timezone.utc).replace(tzinfo=None),
        email=student.email if student else None,
        zitadel_subject=student.zitadel_subject if student else None,
        payload=env.model_dump(mode="json"),
    )
    if student is not None:
        event.user_id = resolve_user_id(db, zitadel_subject=student.zitadel_subject, email=student.email)
        if event.user_id is None:
            event.error = ERROR_UNRESOLVED
    try:
        db.add(event)
        db.flush()
        try:
            apply_event(db, event)
            event.processed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        except UnhandledEventType:
            event.error = ERROR_UNHANDLED
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent delivery of the same event: it is stored, so acknowledge.
        db.rollback()
        return "duplicate", None
    except ProjectionDataError as exc:
        db.rollback()
        return "rejected", f"data: {exc}"
    except Exception:  # noqa: BLE001 - one bad event must never poison the batch
        db.rollback()
        logger.exception("platform event %s/%s failed to ingest", platform, env.event_id)
        return "rejected", "internal: ingest failed"
    return "accepted", None
