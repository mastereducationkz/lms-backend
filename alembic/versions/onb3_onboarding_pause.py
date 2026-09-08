"""curator onboarding: a frozen student's card pauses instead of closing

Two nullable-or-defaulted columns on ``curator_onboarding``. Nothing existing changes
meaning: every current row is "not paused, never paused", which is exactly what
``paused_at IS NULL`` / ``paused_seconds = 0`` say.

* ``paused_at`` — when the current pause started, NULL while the card is running. A paused
  card is still *open* (``ended_at IS NULL``), so it keeps holding the pair's one open-cycle
  slot and the partial unique index needs no change.
* ``paused_seconds`` — how long this cycle has already spent paused. Subtracted by every
  elapsed-time rule (overdue, age), so a card comes back with the clock it left with.

Safe to re-run: both additions are guarded by reflection, and the backfill is a no-op
because the server default already writes 0 for existing rows.

Revision ID: onb3_onboarding_pause
Revises: lr1_cancel_resolution
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "onb3_onboarding_pause"
down_revision: Union[str, Sequence[str], None] = "lr1_cancel_resolution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "curator_onboarding"


def _columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    existing = _columns(TABLE)
    if not existing:
        # Fresh database: create_all builds the current model, including these columns.
        return

    if "paused_at" not in existing:
        op.add_column(TABLE, sa.Column("paused_at", sa.DateTime(), nullable=True))
    if "paused_seconds" not in existing:
        op.add_column(
            TABLE,
            sa.Column(
                "paused_seconds", sa.Integer(), nullable=False, server_default="0"
            ),
        )
    # Belt and braces for a database where the column was added by `create_all` without the
    # default (create_all writes the Python-side default, not a server one).
    op.execute(sa.text(f"UPDATE {TABLE} SET paused_seconds = 0 WHERE paused_seconds IS NULL"))


def downgrade() -> None:
    """Drop the columns. Any pause in flight is lost — a paused card simply reads as running
    again, which is the pre-feature behaviour and not a corruption."""
    existing = _columns(TABLE)
    for column in ("paused_seconds", "paused_at"):
        if column in existing:
            op.drop_column(TABLE, column)
