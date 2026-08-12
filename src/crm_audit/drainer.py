"""Delivering outbox rows to the CRM.

Runs outside the request path, so a CRM that is down or slow costs a teacher nothing. The
outbox row is already durable — written in the same transaction as the mutation — so the
only job here is at-least-once delivery, and the CRM's `idempotency_key` makes "at least
once" safe.

Ordering is by id, which is the emitter's monotonic sequence: a group renamed twice is
delivered rename-then-rename, not the other way round.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from src.crm_audit.models import CrmAuditOutbox

logger = logging.getLogger(__name__)

#: Where the CRM listens, and the shared key that authenticates us to it.
CRM_AUDIT_INGEST_URL = os.getenv("CRM_AUDIT_INGEST_URL", "").strip()
CRM_SERVICE_KEY = os.getenv("LMS_INTERNAL_SERVICE_KEY", "").strip()

BATCH_SIZE = 100
MAX_ATTEMPTS = 10
#: Bounded exponential backoff. Doubling without a ceiling parks a row days into the future
#: after a handful of failures, which turns a transient outage into a permanent gap.
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600


def backoff_for(attempts: int) -> timedelta:
    """Delay before the next attempt, capped."""
    seconds = min(BASE_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)), MAX_BACKOFF_SECONDS)
    return timedelta(seconds=seconds)


def claim_batch(db: Session, *, now: Optional[datetime] = None, limit: int = BATCH_SIZE):
    """Rows due for delivery, oldest first.

    `next_attempt_at IS NULL` is a row that has never been tried, so it is due immediately.
    """
    now = now or datetime.now(timezone.utc)
    return (
        db.query(CrmAuditOutbox)
        .filter(CrmAuditOutbox.status == "pending")
        .filter(
            (CrmAuditOutbox.next_attempt_at.is_(None))
            | (CrmAuditOutbox.next_attempt_at <= now)
        )
        .order_by(CrmAuditOutbox.id.asc())
        .limit(limit)
        .all()
    )


def _default_post(url: str, payload: dict[str, Any], key: str) -> tuple[int, str]:
    import requests

    response = requests.post(
        url,
        json=payload,
        headers={"X-CRM-Service-Key": key},
        timeout=15,
    )
    return response.status_code, (response.text or "")[:500]


def drain_once(
    db: Session,
    *,
    post: Optional[Callable[[str, dict, str], tuple[int, str]]] = None,
    url: Optional[str] = None,
    key: Optional[str] = None,
    now: Optional[datetime] = None,
    limit: int = BATCH_SIZE,
) -> dict[str, int]:
    """Deliver one batch. Returns a small report for the caller to log.

    The whole batch goes in one request and is marked delivered together: the CRM accepts a
    batch atomically and reports duplicates rather than failing on them, so a redelivery
    after a lost response is a no-op rather than a conflict.
    """
    url = url or CRM_AUDIT_INGEST_URL
    key = key or CRM_SERVICE_KEY
    now = now or datetime.now(timezone.utc)

    if not url or not key:
        # Unconfigured is not an error: the outbox keeps accumulating and delivers once the
        # environment is set, rather than dropping events on the floor.
        return {"claimed": 0, "delivered": 0, "failed": 0, "skipped": 1}

    rows = claim_batch(db, now=now, limit=limit)
    if not rows:
        return {"claimed": 0, "delivered": 0, "failed": 0, "skipped": 0}

    poster = post or _default_post
    try:
        status, body = poster(url, {"events": [r.payload for r in rows]}, key)
    except Exception as exc:  # network error, DNS, timeout
        status, body = 0, f"{type(exc).__name__}: {exc}"[:500]

    delivered = 0
    failed = 0
    if 200 <= status < 300:
        for row in rows:
            row.status = "done"
            row.published_at = now
            row.last_error = None
            delivered += 1
    else:
        for row in rows:
            row.attempts = (row.attempts or 0) + 1
            row.last_error = f"HTTP {status}: {body}"
            if row.attempts >= MAX_ATTEMPTS:
                # Parked, never deleted. An audit event that cannot be delivered is a thing
                # somebody must look at, not something to discard quietly.
                row.status = "failed"
            else:
                row.next_attempt_at = now + backoff_for(row.attempts)
            failed += 1
        logger.warning(
            "crm audit delivery failed: status=%s rows=%s error=%s", status, len(rows), body
        )

    db.commit()
    return {"claimed": len(rows), "delivered": delivered, "failed": failed, "skipped": 0}


def run_drain_loop(poll_seconds: int = 20) -> None:
    """Deliver forever, one batch at a time.

    Runs in the scheduler container only, so the API process never double-drains and a slow
    CRM never appears in a teacher's request. Every iteration opens and closes its own
    session: a drainer that holds one open for days accumulates a stale transaction snapshot
    and stops seeing new rows.
    """
    import time

    from src.config import SessionLocal

    logger.info("crm audit drainer started (poll=%ss)", poll_seconds)
    while True:
        try:
            db = SessionLocal()
            try:
                report = drain_once(db)
                if report.get("delivered") or report.get("failed"):
                    logger.info("crm audit drain: %s", report)
            finally:
                db.close()
        except Exception:
            # A drainer that dies leaves the outbox growing silently.
            logger.exception("crm audit drain iteration failed")
        time.sleep(max(1, poll_seconds))
