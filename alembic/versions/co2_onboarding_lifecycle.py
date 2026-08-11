"""curator onboarding lifecycle: cycles, history and notes

Turns ``curator_onboarding`` from "one row per pair, forever" into "one row per *cycle*",
so a student who returns to a curator they were already onboarded by gets a fresh card
instead of reviving (and overwriting) the historical one.

Safety properties, in the order they matter on production data:

* **Nothing is deleted.** Every existing row keeps its id, status and timestamps. The only
  destructive step is dropping the lifetime-uniqueness constraint, which is what blocked
  second cycles in the first place; the new partial unique index is strictly weaker, so no
  existing row can violate it.
* **The backfill is idempotent.** Every UPDATE is guarded by the condition it establishes,
  so re-running the migration (or running it against a partially-migrated database) is a
  no-op rather than a corruption.
* **Open vs closed is derived, not guessed.** Rows the old reconciler had retired
  (``status='cancelled'``) are closed with their existing timestamp; everything else stays
  open, which is precisely what it was. The reconciler closes anything stale on its next
  pass — from real relationship data, not from an assumption baked into a migration.

Revision ID: co2_onboarding_lifecycle
Revises: tlr1_teacher_hourly_rates
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "co2_onboarding_lifecycle"
down_revision: Union[str, Sequence[str], None] = "tlr1_teacher_hourly_rates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "curator_onboarding"


def _inspector():
    return sa.inspect(op.get_bind())


def _columns(table: str) -> set[str]:
    insp = _inspector()
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _indexes(table: str) -> set[str]:
    insp = _inspector()
    if table not in insp.get_table_names():
        return set()
    return {i["name"] for i in insp.get_indexes(table)}


def _unique_constraints(table: str) -> set[str]:
    insp = _inspector()
    if table not in insp.get_table_names():
        return set()
    try:
        return {c["name"] for c in insp.get_unique_constraints(table) if c.get("name")}
    except NotImplementedError:  # pragma: no cover - dialect without reflection support
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if TABLE not in insp.get_table_names():
        # Fresh database: create_all/the earlier revision will build the current model.
        return

    existing = _columns(TABLE)

    # --- 1. lifecycle + operational columns ------------------------------------------------
    if "cycle_no" not in existing:
        op.add_column(
            TABLE, sa.Column("cycle_no", sa.Integer(), nullable=False, server_default="1")
        )
    if "ended_at" not in existing:
        op.add_column(TABLE, sa.Column("ended_at", sa.DateTime(), nullable=True))
    if "end_reason" not in existing:
        op.add_column(TABLE, sa.Column("end_reason", sa.String(length=64), nullable=True))
    if "status_changed_at" not in existing:
        op.add_column(TABLE, sa.Column("status_changed_at", sa.DateTime(), nullable=True))
    if "next_action_at" not in existing:
        op.add_column(TABLE, sa.Column("next_action_at", sa.Date(), nullable=True))
    if "next_action_note" not in existing:
        op.add_column(TABLE, sa.Column("next_action_note", sa.String(length=500), nullable=True))

    # --- 2. idempotent backfill ------------------------------------------------------------
    # Retired rows become closed cycles, carrying the timestamp they were retired at rather
    # than "now" — the history has to say when responsibility actually ended.
    op.execute(
        sa.text(
            f"""
            UPDATE {TABLE}
               SET ended_at = COALESCE(updated_at, created_at),
                   end_reason = 'legacy_cancelled'
             WHERE status = 'cancelled' AND ended_at IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {TABLE}
               SET status_changed_at = COALESCE(updated_at, created_at)
             WHERE status_changed_at IS NULL
            """
        )
    )
    op.execute(sa.text(f"UPDATE {TABLE} SET cycle_no = 1 WHERE cycle_no IS NULL"))

    # --- 3. swap lifetime uniqueness for open-cycle uniqueness -----------------------------
    # The old constraint is what made a second cycle impossible. The replacement allows any
    # number of closed cycles per pair while still permitting exactly one open one.
    if "uq_curator_onboarding_pair" in _unique_constraints(TABLE):
        op.drop_constraint("uq_curator_onboarding_pair", TABLE, type_="unique")
    elif "uq_curator_onboarding_pair" in _indexes(TABLE):
        op.drop_index("uq_curator_onboarding_pair", table_name=TABLE)

    if "uq_curator_onboarding_active" not in _indexes(TABLE):
        op.create_index(
            "uq_curator_onboarding_active",
            TABLE,
            ["curator_id", "student_id"],
            unique=True,
            postgresql_where=sa.text("ended_at IS NULL"),
            sqlite_where=sa.text("ended_at IS NULL"),
        )

    for name, cols in (
        ("ix_curator_onboarding_student_open", ["student_id", "ended_at"]),
        ("ix_curator_onboarding_next_action", ["next_action_at"]),
    ):
        if name not in _indexes(TABLE):
            op.create_index(name, TABLE, cols)

    # --- 4. history + notes ----------------------------------------------------------------
    tables = set(insp.get_table_names())
    if "curator_onboarding_events" not in tables:
        op.create_table(
            "curator_onboarding_events",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column(
                "onboarding_id",
                sa.Integer(),
                sa.ForeignKey("curator_onboarding.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "actor_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("actor_name", sa.String(length=500), nullable=True),
            sa.Column("actor_role", sa.String(length=32), nullable=True),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column("before", sa.JSON(), nullable=True),
            sa.Column("after", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_curator_onboarding_events_onboarding_id",
            "curator_onboarding_events",
            ["onboarding_id"],
        )
        op.create_index(
            "ix_curator_onboarding_events_card_time",
            "curator_onboarding_events",
            ["onboarding_id", "created_at"],
        )
        op.create_index(
            "ix_curator_onboarding_events_created_at",
            "curator_onboarding_events",
            ["created_at"],
        )

    if "curator_onboarding_notes" not in tables:
        op.create_table(
            "curator_onboarding_notes",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column(
                "onboarding_id",
                sa.Integer(),
                sa.ForeignKey("curator_onboarding.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "author_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("author_name", sa.String(length=500), nullable=True),
            sa.Column("author_role", sa.String(length=32), nullable=True),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_curator_onboarding_notes_onboarding_id",
            "curator_onboarding_notes",
            ["onboarding_id"],
        )
        op.create_index(
            "ix_curator_onboarding_notes_card_time",
            "curator_onboarding_notes",
            ["onboarding_id", "created_at"],
        )
        op.create_index(
            "ix_curator_onboarding_notes_created_at",
            "curator_onboarding_notes",
            ["created_at"],
        )


def downgrade() -> None:
    """Reverse the schema change. Cycle history beyond the first is necessarily lost.

    Restoring lifetime uniqueness is only possible if no pair has accumulated more than one
    cycle, so the duplicate-collapsing step below keeps the *open* cycle (the one the board
    is showing) and deletes closed ones for pairs that have several. That is a real data
    loss and the reason to roll forward rather than back if at all avoidable.
    """
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if TABLE not in tables:
        return

    for name in ("curator_onboarding_notes", "curator_onboarding_events"):
        if name in tables:
            op.drop_table(name)

    for name in (
        "ix_curator_onboarding_next_action",
        "ix_curator_onboarding_student_open",
        "uq_curator_onboarding_active",
    ):
        if name in _indexes(TABLE):
            op.drop_index(name, table_name=TABLE)

    # Collapse to one row per pair so the lifetime-unique constraint can be re-created.
    op.execute(
        sa.text(
            f"""
            DELETE FROM {TABLE}
             WHERE id NOT IN (
                   SELECT MAX(id) FROM {TABLE} GROUP BY curator_id, student_id
             )
            """
        )
    )
    if "uq_curator_onboarding_pair" not in _unique_constraints(TABLE):
        op.create_unique_constraint(
            "uq_curator_onboarding_pair", TABLE, ["curator_id", "student_id"]
        )

    existing = _columns(TABLE)
    for column in (
        "next_action_note",
        "next_action_at",
        "status_changed_at",
        "end_reason",
        "ended_at",
        "cycle_no",
    ):
        if column in existing:
            op.drop_column(TABLE, column)
