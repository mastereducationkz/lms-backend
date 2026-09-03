"""``POST /integrations/events`` — platforms push their events here (Platform Integration Pack §2.1).

Auth is a per-platform shared key: ``X-Platform: ielts|sat`` names the sender and
``X-API-Key`` must equal that platform's ``<PLATFORM>_EVENTS_API_KEY``. Auth is checked before
the ingest flag so a key misconfiguration surfaces during the pre-enable probe; while
``PLATFORM_EVENTS_INGEST_ENABLED`` is off the endpoint answers 503, which the senders treat as
"not ready" (reschedule, no retry budget spent).
"""

from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from src.config import get_db
from src.integrations.ingest import ingest_batch
from src.integrations.schemas import PLATFORMS, EventsBatch

router = APIRouter()

_TRUTHY = {"1", "true", "yes", "on"}


def ingest_enabled() -> bool:
    return os.getenv("PLATFORM_EVENTS_INGEST_ENABLED", "").strip().lower() in _TRUTHY


def platform_events_key(platform: str) -> str:
    return os.getenv(f"{platform.upper()}_EVENTS_API_KEY", "").strip()


def require_platform_key(
    x_platform: str = Header(default="", alias="X-Platform"),
    x_api_key: str = Header(default="", alias="X-API-Key"),
) -> str:
    """Resolve and authenticate the sending platform. 401 on any miss (never say which part)."""
    platform = (x_platform or "").strip().lower()
    expected = platform_events_key(platform) if platform in PLATFORMS else ""
    presented = (x_api_key or "").strip()
    if not expected or not presented or not hmac.compare_digest(expected, presented):
        raise HTTPException(status_code=401, detail="Invalid platform key")
    return platform


@router.post("/events")
def ingest_events(
    body: EventsBatch,
    platform: str = Depends(require_platform_key),
    db: Session = Depends(get_db),
) -> dict:
    if not ingest_enabled():
        raise HTTPException(status_code=503, detail="Platform events ingest is disabled")
    return ingest_batch(db, platform, body.events)
