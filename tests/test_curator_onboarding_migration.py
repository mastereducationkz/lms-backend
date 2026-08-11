"""Runs the onboarding-lifecycle migration against a genuine pre-migration table.

Asserting the migration file's *text* would prove nothing. This builds the table as it
exists in production today — lifetime unique constraint and all — fills it with the row
shapes that actually occur there, executes ``upgrade()`` for real, and then checks the three
properties the production run has to have: no data lost, cancelled history closed with the
timestamp it was closed at, and the new invariant enforced by the database rather than by
hope. It then runs the migration a second time, because a deploy that half-applied and
retried must not corrupt anything.
"""
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "co2_onboarding_lifecycle.py"
)

SCHEMA = "onboarding_migration_test"


def _load_migration():
    spec = importlib.util.spec_from_file_location("co2_migration_under_test", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def conn():
    """An isolated schema so the real tables are never touched."""
    from sqlalchemy.exc import OperationalError

    from src.config import engine

    if engine.dialect.name != "postgresql":
        pytest.skip("Migration test targets PostgreSQL, as production does")
    try:
        connection = engine.connect()
    except OperationalError:
        pytest.skip("No database available")

    connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
    connection.execute(sa.text(f'CREATE SCHEMA "{SCHEMA}"'))
    connection.execute(sa.text(f'SET search_path TO "{SCHEMA}"'))
    connection.commit()
    try:
        yield connection
    finally:
        connection.rollback()
        connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        connection.commit()
        connection.close()


def _build_pre_migration_schema(conn):
    """The tables exactly as they are before this revision runs."""
    conn.execute(
        sa.text(
            """
            CREATE TABLE users (
                id SERIAL PRIMARY KEY,
                email VARCHAR,
                name VARCHAR,
                role VARCHAR,
                is_active BOOLEAN DEFAULT TRUE
            );
            CREATE TABLE groups (
                id SERIAL PRIMARY KEY,
                name VARCHAR,
                curator_id INTEGER REFERENCES users(id),
                is_active BOOLEAN DEFAULT TRUE,
                is_over BOOLEAN DEFAULT FALSE
            );
            CREATE TABLE curator_onboarding (
                id SERIAL PRIMARY KEY,
                curator_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                student_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
                status VARCHAR NOT NULL DEFAULT 'new',
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                completed_at TIMESTAMP,
                completed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                CONSTRAINT uq_curator_onboarding_pair UNIQUE (curator_id, student_id)
            );
            CREATE INDEX ix_curator_onboarding_curator_status
                ON curator_onboarding (curator_id, status);
            """
        )
    )
    conn.commit()


def _seed(conn):
    """One row of every shape that exists in production."""
    now = datetime(2026, 8, 1, 12, 0, 0)
    retired_at = now - timedelta(days=30)
    conn.execute(
        sa.text(
            """
            INSERT INTO users (id, email, name, role, is_active) VALUES
              (1,'cur@x','Куратор','curator',TRUE),
              (2,'s1@x','Ученик 1','student',TRUE),
              (3,'s2@x','Ученик 2','student',TRUE),
              (4,'s3@x','Ученик 3','student',TRUE),
              (5,'s4@x','Ученик 4','student',TRUE),
              (6,'head@x','Старший','head_curator',TRUE);
            INSERT INTO groups (id, name, curator_id) VALUES (10,'SAT-1',1);
            """
        )
    )
    conn.execute(
        sa.text(
            """
            INSERT INTO curator_onboarding
                (id, curator_id, student_id, group_id, status, created_at, updated_at,
                 completed_at, completed_by)
            VALUES
              (100, 1, 2, 10, 'new',         :now,        :now,        NULL,  NULL),
              (101, 1, 3, 10, 'in_progress', :now,        :now,        NULL,  NULL),
              (102, 1, 4, 10, 'done',        :now,        :now,        :now,  6),
              (103, 1, 5, 10, 'done',        :now,        :now,        NULL,  NULL),
              (104, 6, 2, 10, 'cancelled',   :old,        :retired,    NULL,  NULL);
            """
        ),
        {"now": now, "old": now - timedelta(days=60), "retired": retired_at},
    )
    conn.commit()
    return now, retired_at


def _run_upgrade(conn):
    module = _load_migration()
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        module.upgrade()
    conn.commit()


def test_upgrade_preserves_every_row_and_closes_only_cancelled_history(conn):
    _build_pre_migration_schema(conn)
    now, retired_at = _seed(conn)

    _run_upgrade(conn)

    rows = {
        r[0]: r
        for r in conn.execute(
            sa.text(
                "SELECT id, status, cycle_no, ended_at, end_reason, status_changed_at "
                "FROM curator_onboarding ORDER BY id"
            )
        ).all()
    }
    assert set(rows) == {100, 101, 102, 103, 104}, "no row may be dropped"

    # Everything that was live stays live.
    for rid in (100, 101, 102, 103):
        assert rows[rid][3] is None, f"row {rid} must remain an open cycle"
        assert rows[rid][2] == 1
        assert rows[rid][5] is not None, "status_changed_at backfilled"

    # Statuses are untouched — including the launch-baseline row (done, completed_by NULL).
    assert rows[100][1] == "new"
    assert rows[101][1] == "in_progress"
    assert rows[102][1] == "done"
    assert rows[103][1] == "done"

    # The retired row is closed, carrying the timestamp it was actually retired at rather
    # than the migration's own clock.
    assert rows[104][1] == "cancelled"
    assert rows[104][3] == retired_at
    assert rows[104][4] == "legacy_cancelled"


def test_upgrade_swaps_lifetime_uniqueness_for_open_cycle_uniqueness(conn):
    _build_pre_migration_schema(conn)
    _seed(conn)
    _run_upgrade(conn)

    constraints = {
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'curator_onboarding'::regclass"
            )
        ).all()
    }
    assert "uq_curator_onboarding_pair" not in constraints, (
        "lifetime uniqueness must be gone — it is what blocked second cycles"
    )

    indexes = {
        r[0]
        for r in conn.execute(
            sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'curator_onboarding'")
        ).all()
    }
    assert "uq_curator_onboarding_active" in indexes

    # A second cycle for a pair that already has a closed one is now possible...
    conn.execute(
        sa.text(
            "INSERT INTO curator_onboarding (curator_id, student_id, status, cycle_no) "
            "VALUES (6, 2, 'new', 2)"
        )
    )
    conn.commit()

    # ...but a second *open* cycle for the same pair is still refused by the database.
    with pytest.raises(sa.exc.IntegrityError):
        conn.execute(
            sa.text(
                "INSERT INTO curator_onboarding (curator_id, student_id, status, cycle_no) "
                "VALUES (6, 2, 'new', 3)"
            )
        )
    conn.rollback()


def test_upgrade_is_idempotent(conn):
    """A retried deploy must be a no-op, not a corruption."""
    _build_pre_migration_schema(conn)
    _, retired_at = _seed(conn)

    _run_upgrade(conn)
    first = conn.execute(
        sa.text("SELECT id, status, cycle_no, ended_at FROM curator_onboarding ORDER BY id")
    ).all()

    _run_upgrade(conn)  # must not raise
    second = conn.execute(
        sa.text("SELECT id, status, cycle_no, ended_at FROM curator_onboarding ORDER BY id")
    ).all()

    assert first == second


def test_new_tables_are_created_with_cascade(conn):
    _build_pre_migration_schema(conn)
    _seed(conn)
    _run_upgrade(conn)

    conn.execute(
        sa.text(
            "INSERT INTO curator_onboarding_notes (onboarding_id, author_id, body) "
            "VALUES (100, 1, 'заметка')"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO curator_onboarding_events (onboarding_id, actor_id, action) "
            "VALUES (100, 1, 'status.changed')"
        )
    )
    conn.commit()

    conn.execute(sa.text("DELETE FROM curator_onboarding WHERE id = 100"))
    conn.commit()
    assert (
        conn.execute(sa.text("SELECT count(*) FROM curator_onboarding_notes")).scalar() == 0
    )
    assert (
        conn.execute(sa.text("SELECT count(*) FROM curator_onboarding_events")).scalar() == 0
    )


def test_downgrade_restores_the_original_shape(conn):
    _build_pre_migration_schema(conn)
    _seed(conn)
    _run_upgrade(conn)

    module = _load_migration()
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        module.downgrade()
    conn.commit()

    # Schema-qualified: an identically named table lives in ``public`` on a real database,
    # and an unqualified information_schema query would silently report its columns.
    columns = {
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'curator_onboarding' AND table_schema = :schema"
            ),
            {"schema": SCHEMA},
        ).all()
    }
    assert "cycle_no" not in columns and "ended_at" not in columns
    constraints = {
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'curator_onboarding'::regclass"
            )
        ).all()
    }
    assert "uq_curator_onboarding_pair" in constraints
    tables = {
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{SCHEMA}'"
            )
        ).all()
    }
    assert "curator_onboarding_notes" not in tables
    assert "curator_onboarding_events" not in tables
