"""Replay dead-lettered student_sync_outbox rows — operator CLI.

The drainer dead-letters an event (status='failed') after 8 transient-failure attempts — today
that is dominated by member.upserted events that 404'd because the student was not yet
provisioned on the target platform. Once the underlying cause is fixed (students provisioned,
emails corrected, consumer deployed), this tool re-queues those rows so the drainer delivers
them; all consumers are idempotent upserts, so replaying already-applied events is harmless.

Usage (inside the backend container / venv, same env as the app):

    python -m src.services.sync_replay                          # summary of failed rows only
    python -m src.services.sync_replay --replay                 # re-queue ALL failed rows
    python -m src.services.sync_replay --replay \
        --event-type member.upserted --like-error "not found"   # targeted re-queue
    python -m src.services.sync_replay --replay --ids 12,15,99  # explicit rows
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from src.config import SessionLocal
from src.courses.models import StudentSyncOutbox


def _failed_query(db, event_type: str | None, like_error: str | None, ids: list[int] | None):
    q = db.query(StudentSyncOutbox).filter(StudentSyncOutbox.status == "failed")
    if event_type:
        q = q.filter(StudentSyncOutbox.event_type == event_type)
    if like_error:
        q = q.filter(StudentSyncOutbox.last_error.ilike(f"%{like_error}%"))
    if ids:
        q = q.filter(StudentSyncOutbox.id.in_(ids))
    return q


def summarize(db) -> dict:
    """Counts of failed rows by event_type (always safe to run)."""
    rows = _failed_query(db, None, None, None).all()
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r.event_type] = by_type.get(r.event_type, 0) + 1
    return {"failed_total": len(rows), "by_event_type": by_type}


def replay(db, *, event_type: str | None = None, like_error: str | None = None,
           ids: list[int] | None = None, dry_run: bool = False) -> dict:
    """Re-queue matching failed rows: status->pending, attempts->0, retry immediately.

    last_error is preserved until the next delivery attempt overwrites it, so an operator can
    still see why the row failed if the replay fails again.
    """
    rows = _failed_query(db, event_type, like_error, ids).order_by(StudentSyncOutbox.id).all()
    if not dry_run:
        now = datetime.now(timezone.utc)
        for r in rows:
            r.status = "pending"
            r.attempts = 0
            r.next_attempt_at = now
            r.target_state = _reset_unfinished_targets(r.target_state)
        db.commit()
    return {"replayed": len(rows), "dry_run": dry_run,
            "ids": [r.id for r in rows[:20]] + (["..."] if len(rows) > 20 else [])}


def _reset_unfinished_targets(state):
    """Keep the targets that already succeeded (ok/skipped) so the replay re-posts only to the
    ones that failed, and give those a fresh attempt budget. NULL stays NULL (attempt all)."""
    if not state:
        return state
    fresh = {}
    for name, entry in state.items():
        entry = entry or {}
        fresh[name] = entry if entry.get("status") in ("ok", "skipped") else {**entry, "attempts": 0}
    return fresh


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay dead-lettered student_sync_outbox rows")
    parser.add_argument("--replay", action="store_true",
                        help="actually re-queue rows (default: just print the failed summary)")
    parser.add_argument("--event-type", default=None, help="only this event_type")
    parser.add_argument("--like-error", default=None,
                        help="only rows whose last_error contains this substring (ILIKE)")
    parser.add_argument("--ids", default=None, help="comma-separated outbox row ids")
    parser.add_argument("--dry-run", action="store_true", help="show what would be re-queued")
    args = parser.parse_args()

    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    db = SessionLocal()
    try:
        print("failed summary:", summarize(db))
        if args.replay or args.dry_run:
            result = replay(db, event_type=args.event_type, like_error=args.like_error,
                            ids=ids, dry_run=args.dry_run and not args.replay)
            print("replay:", result)
    finally:
        db.close()


if __name__ == "__main__":
    main()
