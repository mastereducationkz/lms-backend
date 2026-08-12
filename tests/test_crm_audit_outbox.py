"""The LMS half of CRM audit delivery: the outbox, its triggers, and the drainer.

Runs on in-memory SQLite for the drainer (the state machine is dialect-independent) and
asserts on the trigger SQL as text, because the triggers are PostgreSQL and the suite has no
Postgres. The properties asserted are the ones that would silently break delivery:

* the trigger body cannot roll back the caller's mutation;
* a lesson's `group_ids` is aggregated at the source, since only the LMS knows them;
* backoff is bounded, so a transient outage does not park rows days into the future;
* a row that cannot be delivered is parked, never deleted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.crm_audit import triggers
from src.crm_audit.drainer import (
    MAX_ATTEMPTS,
    MAX_BACKOFF_SECONDS,
    backoff_for,
    claim_batch,
    drain_once,
)
from src.crm_audit.models import CrmAuditOutbox
from src.models.base import Base

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    CrmAuditOutbox.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _row(db, **over):
    row = CrmAuditOutbox(
        event_id=over.pop("event_id", "e1"),
        action=over.pop("action", "group.updated"),
        payload=over.pop("payload", {"event_id": "e1", "action": "group.updated"}),
        status=over.pop("status", "pending"),
        attempts=over.pop("attempts", 0),
        **over,
    )
    db.add(row)
    db.commit()
    return row


# --- trigger SQL ------------------------------------------------------------------------


def test_every_trigger_body_is_shielded_from_rolling_back_the_mutation():
    """Rule 10: an audit failure must not 500 a domain mutation that is otherwise fine."""
    for sql in triggers.ALL_TRIGGER_SQL:
        assert "EXCEPTION WHEN OTHERS" in sql
        assert "RAISE WARNING" in sql


def test_all_four_tables_are_instrumented():
    installed = triggers.install_sql()
    for _, table in triggers.ALL_TRIGGERS:
        assert f"ON {table}" in installed
    assert {t for t, _ in triggers.ALL_TRIGGERS} == {
        "trg_crm_audit_group",
        "trg_crm_audit_member",
        "trg_crm_audit_event",
        "trg_crm_audit_attendance",
    }


def test_installation_is_idempotent():
    """Re-running the migration, or replaying it on a live database, must be safe."""
    installed = triggers.install_sql()
    assert installed.count("CREATE OR REPLACE FUNCTION") == 4
    assert installed.count("DROP TRIGGER IF EXISTS") == 4


def test_a_lessons_groups_are_aggregated_at_the_source():
    """Only this database knows which groups a lesson belongs to."""
    assert "FROM event_groups eg WHERE eg.event_id = NEW.id" in triggers.EVENT_TRIGGER_SQL
    assert "json_agg(eg.group_id)" in triggers.EVENT_TRIGGER_SQL
    # Attendance hangs off the lesson, so it resolves the same way.
    assert "json_agg(eg.group_id)" in triggers.ATTENDANCE_TRIGGER_SQL


def test_only_class_events_are_audited():
    """`events` also holds non-lesson rows; auditing those would be noise."""
    assert "NEW.event_type IS DISTINCT FROM 'class'" in triggers.EVENT_TRIGGER_SQL


def test_cancellation_and_restoration_are_their_own_actions():
    assert "'lesson.cancelled'" in triggers.EVENT_TRIGGER_SQL
    assert "'lesson.restored'" in triggers.EVENT_TRIGGER_SQL


def test_unrelated_column_writes_do_not_enqueue():
    """Without the changed-field guard every unrelated write produces an empty diff."""
    for sql in (triggers.GROUP_TRIGGER_SQL, triggers.EVENT_TRIGGER_SQL):
        assert "IS DISTINCT FROM" in sql
        assert "RETURN NULL" in sql


def test_membership_covers_both_directions():
    assert "'student.group.added'" in triggers.MEMBER_TRIGGER_SQL
    assert "'student.group.removed'" in triggers.MEMBER_TRIGGER_SQL
    assert "AFTER INSERT OR DELETE ON group_students" in triggers.MEMBER_TRIGGER_SQL


def test_every_payload_carries_a_stable_event_id():
    """It becomes the CRM's idempotency key; without it redelivery duplicates."""
    for sql in triggers.ALL_TRIGGER_SQL:
        assert "gen_random_uuid()::text" in sql
        assert "'event_id', v_event_id" in sql


def test_uninstall_drops_both_triggers_and_functions():
    sql = triggers.uninstall_sql()
    assert sql.count("DROP TRIGGER IF EXISTS") == 4
    assert sql.count("DROP FUNCTION IF EXISTS") == 4


# --- backoff ------------------------------------------------------------------------------


def test_backoff_grows_then_stops_growing():
    assert backoff_for(1) == timedelta(seconds=30)
    assert backoff_for(2) == timedelta(seconds=60)
    assert backoff_for(3) == timedelta(seconds=120)
    # Bounded: unbounded doubling parks a row days out after a handful of failures.
    assert backoff_for(50) == timedelta(seconds=MAX_BACKOFF_SECONDS)


def test_backoff_never_returns_a_negative_or_zero_delay():
    for attempts in (-5, 0, 1):
        assert backoff_for(attempts).total_seconds() > 0


# --- claiming -----------------------------------------------------------------------------


def test_a_never_tried_row_is_due_immediately(db):
    _row(db, next_attempt_at=None)
    assert len(claim_batch(db, now=NOW)) == 1


def test_a_row_scheduled_for_later_is_not_claimed(db):
    _row(db, next_attempt_at=NOW + timedelta(minutes=5))
    assert claim_batch(db, now=NOW) == []


def test_a_row_whose_time_has_come_is_claimed(db):
    _row(db, next_attempt_at=NOW - timedelta(seconds=1))
    assert len(claim_batch(db, now=NOW)) == 1


def test_done_and_failed_rows_are_never_reclaimed(db):
    _row(db, event_id="a", status="done")
    _row(db, event_id="b", status="failed")
    assert claim_batch(db, now=NOW) == []


def test_rows_are_claimed_in_emission_order(db):
    for i in range(3):
        _row(db, event_id=f"e{i}")
    claimed = claim_batch(db, now=NOW)
    assert [r.event_id for r in claimed] == ["e0", "e1", "e2"]


# --- delivery -----------------------------------------------------------------------------


def test_a_successful_batch_is_marked_done(db):
    _row(db, event_id="a")
    _row(db, event_id="b")
    sent = {}

    def post(url, payload, key):
        sent["payload"] = payload
        sent["key"] = key
        return 200, "{}"

    report = drain_once(db, post=post, url="http://crm/internal/audit/events", key="k", now=NOW)
    assert report == {"claimed": 2, "delivered": 2, "failed": 0, "skipped": 0}
    assert len(sent["payload"]["events"]) == 2
    assert sent["key"] == "k"
    assert {r.status for r in db.query(CrmAuditOutbox)} == {"done"}


def test_a_failed_batch_is_retried_later_not_lost(db):
    row = _row(db)
    report = drain_once(
        db, post=lambda *_: (500, "boom"), url="http://crm", key="k", now=NOW
    )
    assert report["failed"] == 1
    db.refresh(row)
    assert row.status == "pending"
    assert row.attempts == 1
    # SQLite returns naive datetimes, so compare the wall clock rather than the tzinfo.
    expected = (NOW + backoff_for(1)).replace(tzinfo=None)
    assert row.next_attempt_at.replace(tzinfo=None) == expected
    assert "500" in row.last_error


def test_a_network_error_is_treated_as_a_failure_not_a_crash(db):
    row = _row(db)

    def explode(*_):
        raise ConnectionError("dns")

    report = drain_once(db, post=explode, url="http://crm", key="k", now=NOW)
    assert report["failed"] == 1
    db.refresh(row)
    assert row.status == "pending"
    assert "ConnectionError" in row.last_error


def test_a_row_is_parked_after_the_attempt_ceiling_never_deleted(db):
    row = _row(db, attempts=MAX_ATTEMPTS - 1)
    drain_once(db, post=lambda *_: (500, "boom"), url="http://crm", key="k", now=NOW)
    db.refresh(row)
    assert row.status == "failed"
    # An undeliverable audit event is something somebody must look at.
    assert db.query(CrmAuditOutbox).count() == 1


def test_an_unconfigured_target_accumulates_rather_than_dropping(db):
    _row(db)
    report = drain_once(db, post=lambda *_: (200, ""), url="", key="", now=NOW)
    assert report["skipped"] == 1
    assert db.query(CrmAuditOutbox).one().status == "pending"


def test_an_empty_outbox_does_nothing(db):
    report = drain_once(db, post=lambda *_: (200, ""), url="http://crm", key="k", now=NOW)
    assert report == {"claimed": 0, "delivered": 0, "failed": 0, "skipped": 0}


def test_the_batch_is_capped(db):
    for i in range(10):
        _row(db, event_id=f"e{i}")
    seen = {}

    def post(url, payload, key):
        seen["n"] = len(payload["events"])
        return 200, ""

    drain_once(db, post=post, url="http://crm", key="k", now=NOW, limit=4)
    assert seen["n"] == 4


def test_delivery_sends_the_payload_the_crm_expects(db):
    _row(db, payload={"event_id": "x", "action": "group.updated", "group_ids": [1, 2]})
    captured = {}

    def post(url, payload, key):
        captured.update(payload)
        return 200, ""

    drain_once(db, post=post, url="http://crm", key="k", now=NOW)
    assert "events" in captured
    assert captured["events"][0]["group_ids"] == [1, 2]
