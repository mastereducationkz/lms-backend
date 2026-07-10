"""Cross-platform student-data sync — LMS publisher side (see SSO_SYNC_DESIGN.md).

LMS is the system-of-record for groups: the ``groups`` rows physically live in the LMS DB,
whether they are written by the LMS app or by the CRM's cross-DB writes. Enqueue is therefore
done by a **database trigger** on the ``groups`` table (``p7_student_sync_group_trigger``), which
writes a ``group.upserted`` snapshot into ``student_sync_outbox`` in the *same transaction* as
whichever writer changed the row — so it is atomic and impossible to bypass (the app hook it
replaced could not see CRM writes).

This module is the **drainer**: it publishes pending outbox rows by HTTP-pushing to the target
platforms (SAT/NUET now, IELTS later) idempotently. The drainer is gated by ``SYNC_ENABLED``
(off by default) so it is a strict no-op until enabled. Transport is HTTP (the live X-API-Key
path LMS already uses for scores) — no message broker required.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _outbox_model():
    # Lazy import to avoid an import cycle (courses.models <-> services at load time).
    from src.courses.models import StudentSyncOutbox

    return StudentSyncOutbox

_TRUTHY = {"1", "true", "yes", "on"}
_MAX_ATTEMPTS = 8
# A 503 means the target consumer is deployed but its sync flag is still off. We reschedule
# these WITHOUT spending the retry budget (so a rollout window can't dead-letter valid events)
# and pick them up quickly once the consumer is enabled.
_NOT_READY_RETRY_SECONDS = 60


def sync_enabled() -> bool:
    return os.getenv("SYNC_ENABLED", "").strip().lower() in _TRUTHY


def _sat_base_url() -> str:
    # Reuse the SAT/NUET api/lms base LMS already talks to for scores.
    return os.getenv("SAT_SYNC_URL", "https://api.mastereducation.kz/api/lms").rstrip("/")


def _sat_api_key() -> str:
    return os.getenv("MASTEREDU_API_KEY", "")


# --- drainer (runs in the scheduler container) -------------------------------
# Enqueue is done by the `groups` DB trigger (p7_student_sync_group_trigger), not here, so
# that CRM cross-DB writes are captured too. The drainer only reads/publishes outbox rows.

_ROUTES = {
    # event_type -> (relative path on the SAT api/lms base)
    "group.upserted": "/groups",
    "member.upserted": "/students/membership",
    "member.removed": "/students/membership",
}


def _deliver(row, *, timeout: float = 15.0) -> tuple[str, str]:
    """POST one outbox row to the target. Returns (outcome, detail).

    outcome is one of:
      "ok"        — delivered (2xx); mark done.
      "not_ready" — target reachable but its sync flag is off (503); reschedule without
                    spending the retry budget so we never dead-letter a valid event during
                    a rollout window. Self-heals once the consumer is enabled.
      "retry"     — transient failure (5xx/timeout/other); spend an attempt, dead-letter
                    after _MAX_ATTEMPTS.
    """
    path = _ROUTES.get(row.event_type)
    if path is None:
        return "retry", f"no route for event_type {row.event_type}"
    url = f"{_sat_base_url()}{path}"
    try:
        resp = httpx.post(
            url,
            json=row.payload,
            headers={"X-API-Key": _sat_api_key(), "Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        return "retry", f"transport error: {exc}"
    if resp.status_code in (200, 201, 204):
        return "ok", "ok"
    if resp.status_code == 503:
        return "not_ready", f"HTTP 503: {resp.text[:200]}"
    return "retry", f"HTTP {resp.status_code}: {resp.text[:200]}"


def run_drain_loop(stop_event=None, poll_seconds: int = 15) -> None:
    """Background loop for the scheduler container: drain the outbox every ``poll_seconds``.

    No-op unless ``SYNC_ENABLED``. Opens its own short-lived session per pass and never
    raises out of the loop, so a transient DB/target failure can't kill the scheduler.
    """
    import time as _time

    from src.config import SessionLocal

    logger.info("student-sync drainer loop started (poll=%ss, enabled=%s)", poll_seconds, sync_enabled())
    while stop_event is None or not stop_event.is_set():
        try:
            if sync_enabled():
                db = SessionLocal()
                try:
                    result = drain_outbox(db)
                    if result["published"] or result["failed"]:
                        logger.info("student-sync drain: %s", result)
                finally:
                    db.close()
        except Exception as exc:  # pragma: no cover - loop must never die
            logger.warning("student-sync drain pass failed (non-fatal): %s", exc)
        _time.sleep(poll_seconds)


def drain_outbox(db: Session, *, batch: int = 50) -> dict:
    """Publish pending outbox rows. Returns {published, failed, retried}. Safe to call repeatedly."""
    Model = _outbox_model()
    now = datetime.now(timezone.utc)
    rows = (
        db.query(Model)
        .filter(
            Model.status == "pending",
            (Model.next_attempt_at.is_(None)) | (Model.next_attempt_at <= now),
        )
        .order_by(Model.id.asc())
        .limit(batch)
        .all()
    )
    published = failed = retried = 0
    for row in rows:
        outcome, detail = _deliver(row)
        if outcome == "ok":
            row.status = "done"
            row.published_at = datetime.now(timezone.utc)
            row.last_error = None
            published += 1
        elif outcome == "not_ready":
            # Consumer deployed but flag off: reschedule without spending the budget so a
            # rollout window can't dead-letter valid events. Stays pending; flushes on enable.
            row.last_error = detail
            row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=_NOT_READY_RETRY_SECONDS)
            retried += 1
        else:  # "retry"
            row.attempts = (row.attempts or 0) + 1
            row.last_error = detail
            if row.attempts >= _MAX_ATTEMPTS:
                row.status = "failed"  # surfaced for an operator; not retried
                failed += 1
            else:
                backoff = min(3600, 30 * (2 ** (row.attempts - 1)))
                row.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
                retried += 1
        db.commit()
    return {"published": published, "failed": failed, "retried": retried}


# --- database trigger DDL (installed by the p7 migration) --------------------
# Enqueue lives in the DB, not the app, so EVERY writer of the `groups` table is captured:
# the LMS app AND the CRM's cross-DB writes (crm-master writes groups via lms_write_session_scope)
# AND any manual SQL. The trigger runs inside the writer's own transaction, so the outbox row is
# atomic with the group change. Kept here (next to the drainer) so the payload shape the drainer
# publishes and the trigger produces stay in lockstep; imported verbatim by the migration and tests.
#
# CRITICAL: the INSERT is wrapped in EXCEPTION WHEN OTHERS so a sync-bookkeeping failure can NEVER
# roll back a group write (the trigger is in the hot path of admin + CRM group edits). It fires on
# UPDATE only when a synced field actually changed, to avoid enqueuing on unrelated column edits.
# Requires PostgreSQL 13+ for the built-in gen_random_uuid().

GROUP_SYNC_FUNCTION = "student_sync_enqueue_group"
GROUP_SYNC_TRIGGER = "trg_student_sync_group"

GROUP_SYNC_TRIGGER_UP_SQL = f"""
CREATE OR REPLACE FUNCTION {GROUP_SYNC_FUNCTION}() RETURNS trigger AS $fn$
DECLARE
    v_event_id text;
BEGIN
    -- The ENTIRE body (including the changed-field guard, which reads NEW/OLD columns) runs inside
    -- one EXCEPTION shield, so nothing this trigger does — not even a future schema drift that made a
    -- referenced column disappear — can raise into and roll back the caller's group write.
    BEGIN
        -- On UPDATE, only enqueue when a field we actually sync changed.
        IF (TG_OP = 'UPDATE') AND NOT (
            NEW.name IS DISTINCT FROM OLD.name
            OR NEW.program_type IS DISTINCT FROM OLD.program_type
            OR NEW.is_active IS DISTINCT FROM OLD.is_active
        ) THEN
            RETURN NULL;
        END IF;

        v_event_id := gen_random_uuid()::text;
        INSERT INTO student_sync_outbox (event_id, event_type, payload, status, attempts, created_at)
        VALUES (
            v_event_id,
            'group.upserted',
            json_build_object(
                'event_id', v_event_id,
                'event_type', 'group.upserted',
                'source', 'lms_db_trigger',
                'group', json_build_object(
                    'lms_group_id', NEW.id,
                    'name', NEW.name,
                    'program_type', NEW.program_type,
                    -- is_active is nullable in the LMS model; never emit JSON null (the SAT
                    -- consumer binds a non-nullable bool). Absent/NULL => active.
                    'is_active', COALESCE(NEW.is_active, true)
                )
            ),
            'pending',
            0,
            (now() AT TIME ZONE 'utc')
        );
    EXCEPTION WHEN OTHERS THEN
        -- Never break the group write because of sync bookkeeping. (Concatenated, not a '%'
        -- format string, so the DDL carries no '%' that a raw DBAPI execute would misread.)
        RAISE WARNING USING MESSAGE =
            'student_sync_enqueue_group failed for group ' || COALESCE(NEW.id::text, '?') || ': ' || SQLERRM;
    END;

    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS {GROUP_SYNC_TRIGGER} ON groups;
CREATE TRIGGER {GROUP_SYNC_TRIGGER}
    AFTER INSERT OR UPDATE ON groups
    FOR EACH ROW EXECUTE FUNCTION {GROUP_SYNC_FUNCTION}();
"""

GROUP_SYNC_TRIGGER_DOWN_SQL = f"""
DROP TRIGGER IF EXISTS {GROUP_SYNC_TRIGGER} ON groups;
DROP FUNCTION IF EXISTS {GROUP_SYNC_FUNCTION}();
"""


# --- membership trigger DDL (installed by the p8 migration) ------------------
# Same design as the group trigger, on `group_students` (the student<->group M2M). Fires on
# INSERT (student added to a group => member.upserted) and DELETE (removed => member.removed).
# LMS mutates membership as delete+insert (not update), so INSERT/DELETE is sufficient. The
# function joins to `users` (email, central_auth_user_id, name — the consumer's match keys) and
# `groups` (program_type, name — so the consumer can route/skip and derive the label). Fully
# EXCEPTION-shielded so it can never roll back a membership write (hot path of admin + CRM
# enrollment). Enqueues for ALL programs; the consumer skips non-SAT/NUET (200 skipped) — matches
# the group trigger's "capture-all, skip-at-consumer" philosophy. Requires PostgreSQL 13+.

MEMBER_SYNC_FUNCTION = "student_sync_enqueue_member"
MEMBER_SYNC_TRIGGER = "trg_student_sync_member"

MEMBER_SYNC_TRIGGER_UP_SQL = f"""
CREATE OR REPLACE FUNCTION {MEMBER_SYNC_FUNCTION}() RETURNS trigger AS $fn$
DECLARE
    v_event_id text;
    v_evt      text;
    v_gid      int;
    v_sid      int;
    v_email    text;
    v_cauid    text;
    v_name     text;
    v_program  text;
    v_gname    text;
BEGIN
    BEGIN
        IF TG_OP = 'DELETE' THEN
            v_evt := 'member.removed'; v_gid := OLD.group_id; v_sid := OLD.student_id;
        ELSE
            v_evt := 'member.upserted'; v_gid := NEW.group_id; v_sid := NEW.student_id;
        END IF;

        SELECT email, central_auth_user_id, name
          INTO v_email, v_cauid, v_name FROM users WHERE id = v_sid;
        SELECT program_type, name
          INTO v_program, v_gname FROM groups WHERE id = v_gid;

        v_event_id := gen_random_uuid()::text;
        INSERT INTO student_sync_outbox (event_id, event_type, payload, status, attempts, created_at)
        VALUES (
            v_event_id,
            v_evt,
            json_build_object(
                'event_id', v_event_id,
                'event_type', v_evt,
                'source', 'lms_db_trigger',
                'lms_group_id', v_gid,
                'group_name', v_gname,
                'program_type', v_program,
                'student', json_build_object(
                    'lms_student_id', v_sid,
                    'email', v_email,
                    'central_auth_user_id', v_cauid,
                    'name', v_name
                )
            ),
            'pending',
            0,
            (now() AT TIME ZONE 'utc')
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING USING MESSAGE =
            'student_sync_enqueue_member failed (g=' || COALESCE(v_gid::text, '?')
            || ', s=' || COALESCE(v_sid::text, '?') || '): ' || SQLERRM;
    END;

    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS {MEMBER_SYNC_TRIGGER} ON group_students;
CREATE TRIGGER {MEMBER_SYNC_TRIGGER}
    AFTER INSERT OR DELETE ON group_students
    FOR EACH ROW EXECUTE FUNCTION {MEMBER_SYNC_FUNCTION}();
"""

MEMBER_SYNC_TRIGGER_DOWN_SQL = f"""
DROP TRIGGER IF EXISTS {MEMBER_SYNC_TRIGGER} ON group_students;
DROP FUNCTION IF EXISTS {MEMBER_SYNC_FUNCTION}();
"""
